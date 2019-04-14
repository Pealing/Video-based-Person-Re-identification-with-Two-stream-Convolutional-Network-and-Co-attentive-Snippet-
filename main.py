# system tool
from __future__ import print_function, absolute_import
import argparse
import os.path as osp
import sys

# computation tool
import torch
import numpy as np

# device tool
import torch.backends.cudnn as cudnn

# utilis
from utils.logging import Logger
from reid import models
from utils.serialization import load_checkpoint, save_cnn_checkpoint, save_att_checkpoint, save_cls_checkpoint
from reid.loss import PairLoss, OIMLoss
from reid.data import get_data
from reid.train import SEQTrainer
from reid.evaluator import ATTEvaluator

from tensorboardX import SummaryWriter


import os
# import data_manager

def main(args):
    if not os.path.exists(args.logs_dir):
        os.mkdir(args.logs_dir)
    if not os.path.exists(args.tensorboard_dir):
        os.mkdir(args.tensorboard_dir)
    tensorboardWrite = SummaryWriter(log_dir = args.tensorboard_dir)
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_devices
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    cudnn.benchmark = True
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # log file
    if args.evaluate == 1:
        sys.stdout = Logger(osp.join(args.logs_dir, 'log_test.txt'))
    else:
        sys.stdout = Logger(osp.join(args.logs_dir, 'log_train.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    print("Initializing dataset {}".format(args.dataset))
    # from reid.data import get_data ,
    dataset, num_classes, train_loader, query_loader, gallery_loader = \
        get_data(args, args.dataset, args.split, args.data_dir,
                 args.batch_size, args.seq_len, args.seq_srd,
                 args.workers)
    print('[len] train: {}, query: {}, gallery: {}'.format(*list(map(len, [train_loader, query_loader, gallery_loader]))))

    # create CNN model
    # cnn_model = models.create(args.a1, args.flow1, args.flow2, num_features=args.features, dropout=args.dropout)
    cnn_model_flow = [models.create(args.a1, args.flow1, num_features=args.features, dropout=args.dropout)]
    if any(args.flow2):
        cnn_model_flow.append(models.create(args.a1, args.flow2, num_features=args.features, dropout=args.dropout))
    # cnn_model_flow1 = cnn_model_flow1.cuda()
    # cnn_model_flow2 = cnn_model_flow2.cuda()


    # create ATT model
    input_num = cnn_model_flow[0].feat.in_features  # 2048
    output_num = args.features  # 128
    att_model = models.create(args.a2, input_num, output_num)
    # att_model.cuda()

    # # ------peixian:tow attmodel------
    # att_model_flow1 = models.create(args.a2, input_num, output_num)
    # att_model_flow2 = models.create(args.a2, input_num, output_num)
    # # --------------------------------

    # create classifier model
    class_num = 2
    classifier_model = models.create(args.a3,  output_num, class_num)
    # classifier_model.cuda()

    # CUDA acceleration model

    # cnn_model = torch.nn.DataParallel(cnn_model).to(device)
    # # ------peixian:tow attmodel------
    # for att_model in [att_model_flow1, att_model_flow2]:
    #     att_model = att_model.to(device)
    # # --------------------------------
    att_model = att_model.cuda()
    classifier_model = classifier_model.cuda()

    # cnn_model = torch.nn.DataParallel(cnn_model).cuda()
    # cnn_model_flow1 = torch.nn.DataParallel(cnn_model_flow1,device_ids=[0,1,2])
    # cnn_model_flow2 = torch.nn.DataParallel(cnn_model_flow2,device_ids=[0,1,2])
    
    # 
    cnn_model_flow[0].cuda()
    cnn_model_flow[0] = torch.nn.DataParallel(cnn_model_flow[0],device_ids=[0])
    if len(cnn_model_flow) > 1:
        cnn_model_flow[1].cuda()
        cnn_model_flow[1] = torch.nn.DataParallel(cnn_model_flow[1],device_ids=[0])



    # att_model = torch.nn.DataParallel(att_model,device_ids=[1,2,3])
    # classifier_model = torch.nn.DataParallel(classifier_model,device_ids=[1,2,3])


    criterion_oim = OIMLoss(args.features, num_classes,
                            scalar=args.oim_scalar, momentum=args.oim_momentum)
    criterion_veri = PairLoss(args.sampling_rate)
    criterion_oim.cuda()
    criterion_veri.cuda()

    # criterion_oim.cuda()
    # criterion_veri.cuda()

    # Optimizer
    optimizer1 = []
    # cnn_model_flow = [cnn_model_flow1, cnn_model_flow2]
    for cnn_model in range(len(cnn_model_flow)):
        base_param_ids = set(map(id, cnn_model_flow[cnn_model].module.base.parameters()))
        new_params = [p for p in cnn_model_flow[cnn_model].module.parameters() if
                    id(p) not in base_param_ids]

        param_groups1 = [
            {'params': cnn_model_flow[cnn_model].module.base.parameters(), 'lr_mult': 1},
            {'params': new_params, 'lr_mult': 1}]

        optimizer1.append(torch.optim.SGD(param_groups1, lr=args.lr1,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    nesterov=True))
    
    param_groups2 = [
        {'params': att_model.parameters(), 'lr_mult': 1},
        {'params': classifier_model.parameters(), 'lr_mult': 1}]                        
    optimizer2 = torch.optim.SGD(param_groups2, lr=args.lr2,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,
                                nesterov=True)
    # optimizer1 = torch.optim.Adam(param_groups1, lr=args.lr1, weight_decay=args.weight_decay)
    #
    # optimizer2 = torch.optim.Adam(param_groups2, lr=args.lr2, weight_decay=args.weight_decay)

    # Schedule Learning rate
    def adjust_lr1(epoch):
        lr = args.lr1 * (0.1 ** (epoch/args.lr1step))
        print(lr)
        for o in optimizer1:
            for g in o.param_groups:
                g['lr'] = lr * g.get('lr_mult', 1)

    def adjust_lr2(epoch):
        lr = args.lr2 * (0.01 ** (epoch//args.lr2step))
        print(lr)
        for g in optimizer2.param_groups:
            g['lr'] = lr * g.get('lr_mult', 1)
        # # peixian:  two attmodel:
        # for o in optimizer2:
        #     for g in o.param_groups:
        #         g['lr'] = lr * g.get('lr_mult', 1)
        # #

    def adjust_lr3(epoch):
        lr = args.lr3 * (0.000001 ** (epoch //args.lr3step))
        print(lr)
        return lr

    # Trainer
    trainer = SEQTrainer(cnn_model_flow, att_model, classifier_model, criterion_veri, criterion_oim, args.lr3, args.flow1rate)


    # Evaluator
    evaluator = ATTEvaluator(cnn_model_flow, att_model, classifier_model, args.flow1rate)

    best_top1 = 0
    if args.evaluate == 1 or args.pretrain == 1:  # evaluate
        for cnn_model in range(len(cnn_model_flow)):
            checkpoint = load_checkpoint(osp.join(args.logs_dir, 'cnnmodel_best_flow' + str(cnn_model) + '.pth.tar'))
            cnn_model_flow[cnn_model].module.load_state_dict(checkpoint['state_dict'])

        checkpoint = load_checkpoint(osp.join(args.logs_dir, 'attmodel_best.pth.tar'))
        att_model.load_state_dict(checkpoint['state_dict'])

        checkpoint = load_checkpoint(osp.join(args.logs_dir, 'clsmodel_best.pth.tar'))
        classifier_model.load_state_dict(checkpoint['state_dict'])

        top1 = evaluator.evaluate(query_loader, gallery_loader, dataset.queryinfo, dataset.galleryinfo)
        # top1 = evaluator.evaluate(query_loader, gallery_loader,dataset.num_tracklet)

    if args.evaluate == 0:
        for epoch in range(args.start_epoch, args.epochs):
            adjust_lr1(epoch)
            adjust_lr2(epoch)
            rate = adjust_lr3(epoch)
            trainer.train(epoch, train_loader, optimizer1, optimizer2, rate,tensorboardWrite)

            if (epoch+1) % 1 == 0 or (epoch+1) == args.epochs:

                top1 = evaluator.evaluate(query_loader, gallery_loader, dataset.queryinfo, dataset.galleryinfo)

                is_best = top1 > best_top1
                if is_best:
                    best_top1 = top1
                for cnn_model in range(len(cnn_model_flow)):
                    save_cnn_checkpoint({
                        'state_dict': cnn_model_flow[cnn_model].module.state_dict(),
                        'epoch': epoch + 1,
                        'best_top1': best_top1,
                    }, is_best, index=cnn_model, fpath=osp.join(args.logs_dir, 'cnn_checkpoint_flow'+str(cnn_model)+'.pth.tar'))

                save_att_checkpoint({
                    'state_dict': att_model.state_dict(),
                    'epoch': epoch + 1,
                    'best_top1': best_top1,
                }, is_best, fpath=osp.join(args.logs_dir, 'att_checkpoint.pth.tar'))

                save_cls_checkpoint({
                    'state_dict': classifier_model.state_dict(),
                    'epoch': epoch + 1,
                    'best_top1': best_top1,
                }, is_best, fpath=osp.join(args.logs_dir, 'cls_checkpoint.pth.tar'))


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="ID Training ResNet Model")

    # DATA
    parser.add_argument('-d', '--dataset', type=str, default='mars',
                        choices=['ilidsvid','prid','mars'])
    parser.add_argument('-b', '--batch-size', type=int, default=8)

    parser.add_argument('-j', '--workers', type=int, default=4)

    parser.add_argument('--seq_len', type=int, default=8)

    parser.add_argument('--seq_srd', type=int, default=4)

    parser.add_argument('--split', type=int, default=0)

    # MODEL
    # CNN model
    parser.add_argument('--a1', '--arch_1', type=str, default='resnet50',
                        choices=models.names())
    parser.add_argument('--features', type=int, default=128)
    parser.add_argument('--dropout', type=float, default=0.0)

    # Attention model
    parser.add_argument('--a2', '--arch_2', type=str, default='attmodel',
                        choices=models.names())
    # Classifier_model
    parser.add_argument('--a3', '--arch_3', type=str, default='classifier',
                        choices=models.names())

    # Criterion model
    parser.add_argument('--loss', type=str, default='oim',
                        choices=['xentropy', 'oim', 'triplet'])
    parser.add_argument('--oim-scalar', type=float, default=30)
    parser.add_argument('--oim-momentum', type=float, default=0.5)
    parser.add_argument('--sampling-rate', type=int, default=3)

    # OPTIMIZER
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--lr1', type=float, default=0.001)
    parser.add_argument('--lr2', type=float, default=0.001)
    parser.add_argument('--lr3', type=float, default=1.0)

    parser.add_argument('--lr1step', type=float, default=20)
    parser.add_argument('--lr2step', type=float, default=10)
    parser.add_argument('--lr3step', type=float, default=30)

    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    parser.add_argument('--cnn_resume', type=str, default='', metavar='PATH')
    parser.add_argument('--num-instances', type=int, default=4,
                    help="number of instances per identity")

    # TRAINER
    parser.add_argument('--start-epoch', type=int, default=0)
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--evaluate', type=int, default=0)
    parser.add_argument('--pretrain', type=int, default=0)
    # misc
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'data'))
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'Adam8_4'))
    
    # GPU
    parser.add_argument('--use-gpu', default = True)
    parser.add_argument('--gpu-devices', default = "0,1")

    # flow
    flow_component = ['rgb', 'optical', 'pose']
    parser.add_argument('-f1', '--flow1', nargs='+', default=[], choices=flow_component, required=True)
    parser.add_argument('-f2', '--flow2', nargs='*', default=[], choices=flow_component)
    parser.add_argument('--flow1rate',type=float, default=0.9)

    parser.add_argument('--tensorboard-dir', type=str, default='tensorboard-log')
    args = parser.parse_args()

    # main function
    main(args)
