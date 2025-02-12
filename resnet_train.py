# ------------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Bin Xiao (Bin.Xiao@microsoft.com)
# Modified by Chenyan Wu (czw390@psu.edu)
# ------------------------------------------------------------------------------

import argparse
import os
import pprint
import shutil

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
from tensorboardX import SummaryWriter

from config import cfg
from config import update_config

from core.loss import JointsMSELoss

from core.function import train
from core.function import validate

from utils.utils import get_optimizer
from utils.utils import save_checkpoint
from utils.utils import create_logger
from utils.utils import get_model_summary
from utils.unsharp_masking import UnsharpMasking

import dataset
import models.resnet as resnet
from math import ceil

def parse_args():
    parser = argparse.ArgumentParser(description='Train keypoints network')
    # general
    parser.add_argument('--cfg',
                        help='experiment configure file name',
                        required=True,
                        type=str)

    parser.add_argument('opts',
                        help="Modify config options using the command-line",
                        default=None,
                        nargs=argparse.REMAINDER)

    # philly
    parser.add_argument('--modelDir',
                        help='model directory',
                        type=str,
                        default='')
    
    parser.add_argument('--logDir',
                        help='log directory',
                        type=str,
                        default='')
    
    parser.add_argument('--dataDir',
                        help='data directory',
                        type=str,
                        default='')
    
    parser.add_argument('--prevModelDir',
                        help='prev Model directory',
                        type=str,
                        default='')

    args = parser.parse_args()

    return args


def main():
    args = parse_args()
    update_config(cfg, args)

    logger, final_output_dir, tb_log_dir = create_logger(
        cfg, args.cfg, 'train')

    logger.info(pprint.pformat(args))
    logger.info(cfg)

    # cudnn related setting
    cudnn.benchmark = cfg.CUDNN.BENCHMARK
    torch.backends.cudnn.deterministic = cfg.CUDNN.DETERMINISTIC
    torch.backends.cudnn.enabled = cfg.CUDNN.ENABLED

    model = resnet.ResNet50(num_classes=72)

    # TODO: Add support for selecting between resnet & HRNet in config
    #model = eval('models.'+cfg.MODEL.NAME+'.get_pose_net')(
    #    cfg, is_train=True
    #)

    # copy model file
    this_dir = os.path.dirname(__file__)
    shutil.copy2(
        os.path.join(this_dir, 'models', cfg.MODEL.NAME + '.py'),
        final_output_dir)
    # logger.info(pprint.pformat(model))

    writer_dict = {
        'writer': SummaryWriter(logdir=tb_log_dir),
        'train_global_steps': 0,
        'valid_global_steps': 0,
    }

    dump_input = torch.rand(
        (1, 3, cfg.MODEL.IMAGE_SIZE[1], cfg.MODEL.IMAGE_SIZE[0])
    )
    # writer_dict['writer'].add_graph(model, (dump_input, ), operator_export_type = "RAW")

    logger.info(get_model_summary(model, dump_input))

    # load pretrained model
    # pre_model = os.path.join(final_output_dir, 'model_best.pth')
    # logger.info("=> loading checkpoint '{}'".format(pre_model))
    # checkpoint = torch.load(pre_model)
    # if 'state_dict' in checkpoint:
    #     model.load_state_dict(checkpoint['state_dict'], strict=True)
    # else:
    #     model.load_state_dict(checkpoint, strict=True)

    model = torch.nn.DataParallel(model, device_ids=cfg.GPUS).cuda()

    # criterions = nn.CrossEntropyLoss()
    criterions = {}
    criterions['2d_pose_loss'] = JointsMSELoss(
        use_target_weight=cfg.LOSS.USE_TARGET_WEIGHT
    ).cuda()
    criterions['hoe_loss'] = torch.nn.MSELoss().cuda()

    # Data loading code
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )

    # Dataset transforms
    transforms_compose = [
        transforms.ToTensor(),
        normalize
    ]

    # Include preprocessing options to dataset transforms
    if cfg.PREPROCESSING.EQUALIZATION: # Histogram Equalization
        #transforms_compose.append(transforms.functional.equalize)
        transforms_compose.insert(0, transforms.functional.equalize)
        transforms_compose.insert(0, transforms.ToPILImage())
    if cfg.PREPROCESSING.GAUSSIAN_BLUR: # Gaussian blur
        transforms_compose.append(transforms.GaussianBlur(9, sigma=(0.1, 2.0)))
    if cfg.PREPROCESSING.UNSHARP_MASKING: # Unsharp Masking
        transforms_compose.append(UnsharpMasking())

    train_dataset = dataset.COCO_HOE_Dataset(cfg, cfg.DATASET.TRAIN_ROOT, True, transforms.Compose([transforms_compose]))

    valid_dataset = dataset.COCO_HOE_Dataset(cfg, cfg.DATASET.TRAIN_ROOT, False, transforms.Compose([transforms_compose]))

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=cfg.TRAIN.BATCH_SIZE_PER_GPU*len(cfg.GPUS),
        shuffle=cfg.TRAIN.SHUFFLE,
        num_workers=cfg.WORKERS,
        pin_memory=cfg.PIN_MEMORY
    )
    valid_loader = torch.utils.data.DataLoader(
        valid_dataset,
        batch_size=cfg.TEST.BATCH_SIZE_PER_GPU*len(cfg.GPUS),
        shuffle=False,
        num_workers=cfg.WORKERS,
        pin_memory=cfg.PIN_MEMORY
    )

    best_perf = 200.0
    best_model = False
    last_epoch = -1
    optimizer = get_optimizer(cfg, model)
    begin_epoch = cfg.TRAIN.BEGIN_EPOCH

    checkpoint_file = os.path.join(
        final_output_dir, 'checkpoint.pth'
    )

    if cfg.AUTO_RESUME and os.path.exists(checkpoint_file):
        logger.info("=> loading checkpoint '{}'".format(checkpoint_file))
        checkpoint = torch.load(checkpoint_file)
        begin_epoch = checkpoint['epoch']
        best_perf = checkpoint['perf']
        last_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])

        optimizer.load_state_dict(checkpoint['optimizer'])
        logger.info("=> loaded checkpoint '{}' (epoch {})".format(
            checkpoint_file, checkpoint['epoch']))

    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, cfg.TRAIN.LR_STEP, cfg.TRAIN.LR_FACTOR,
        last_epoch=last_epoch
    )

    for epoch in range(begin_epoch, cfg.TRAIN.END_EPOCH):
        print(epoch)
        # train for one epoch
        # evaluate on validation set

        train(cfg, train_loader, train_dataset, model, criterions, optimizer, epoch,
              final_output_dir, tb_log_dir, writer_dict)

        perf_indicator = validate(
            cfg, valid_loader, valid_dataset, model, criterions,
            final_output_dir, tb_log_dir, writer_dict
        )

        if perf_indicator <= best_perf:
            best_perf = perf_indicator
            best_model = True
        else:
            best_model = False

        lr_scheduler.step()
        logger.info('=> saving checkpoint to {}'.format(final_output_dir))
        logger.info('best_model{}'.format(best_perf))
        save_checkpoint({
            'epoch': epoch + 1,
            'model': cfg.MODEL.NAME,
            'state_dict': model.state_dict(),
            'best_state_dict': model.module.state_dict(),
            'perf': best_perf,
            'optimizer': optimizer.state_dict(),
        }, best_model, final_output_dir)

    final_model_state_file = os.path.join(
        final_output_dir, 'final_state.pth'
    )
    print('=> saving final model state to {}'.format(final_model_state_file))
    logger.info('=> saving final model state to {}'.format(
        final_model_state_file)
    )
    torch.save(model.module.state_dict(), final_model_state_file)
    writer_dict['writer'].close()


if __name__ == '__main__':
    main()
