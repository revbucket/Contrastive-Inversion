#!/usr/bin/env python

import argparse
import numpy as np
import torch
import torch.nn.functional as F
import torch.nn as nn

from utils import *

from pytorch_lightning import Trainer, LightningModule, seed_everything
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.metrics import Accuracy
from torch.utils.data  import DataLoader

from noisy_clip_dataparallel import NoisyCLIP, ImageNetCLIPDataset
from linear_probe import LinearProbe

class NoisyCLIPTesting(LightningModule):
    """
    Wrapper for the original NoisyCLIP, in order to implement testing functions.
    The goal is to obtain predictions using a saved instance of NoisyCLIP, and evaluate these on the test set.
    """
    def __init__(self, args, ckpt_file):
        super(NoisyCLIPTesting,self).__init__()
        self.backbone = NoisyCLIP.load_from_checkpoint(ckpt_file).eval()
        self.test_top_1 = Accuracy(top_k=1)
        self.test_top_5 = Accuracy(top_k=5)

    def forward(self, x):
        embed = self.backbone.encode_noisy_image(x)
        return self.backbone(embed)[0]

    def test_step(self, batch, batch_idx):
        x, y = batch

        logits = self.forward(x)
        logits = logits.float()
        pred_probs = logits.softmax(dim=-1)

        self.log("test_top_1", self.test_top_1(pred_probs, y), prog_bar=False, logger=False)
        self.log("test_top_5", self.test_top_5(pred_probs, y), prog_bar=False, logger=False)

    def test_epoch_end(self, outputs):
        self.log("test_top_1", self.test_top_1.compute(), prog_bar=True, logger=True)
        self.log("test_top_5", self.test_top_5.compute(), prog_bar=True, logger=True)
        self.test_top_1.reset()
        self.test_top_5.reset()

def grab_config():
    """
    Function used to retrieve arguments from the configuration file.
    """
    parser = argparse.ArgumentParser(description="NoisyCLIP")

    parser.add_argument('--config_file')
    parser.add_argument('--ckpt_file')

    config = yaml_config_hook(parser.parse_args().config_file)
    for k, v in config.items():
        parser.add_argument(f"--{k}", default=v, type=type(v))

    args = parser.parse_args()

    return args

def zeroshot_eval():
    args = grab_config()
    args.distributed_backend='ddp'
    seed_everything(args.seed)

    logger = TensorBoardLogger(
        save_dir=args.logdir,
        version=args.experiment_name,
        name='NoisyCLIP_Logs'
    )

    checkpoint_file = args.ckpt_file

    trainer = Trainer.from_argparse_args(args, logger=logger)

    dataset = ImageNetCLIPDataset(args)
    dataset.setup()
    model = NoisyCLIPTesting(args, checkpoint_file)

    trainer.test(model, datamodule=dataset)

if __name__ == "__main__":
    zeroshot_eval()
