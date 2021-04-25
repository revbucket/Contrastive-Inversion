#!/usr/bin/env python

import sys
import os
import argparse
import numpy as np
import torch
from torch import Tensor
import typing
import torch.nn.functional as F
import model
import clip
import copy
import pickle
from tqdm import tqdm

import torch
import torchvision

from utils import *

from pytorch_lightning import Trainer, LightningModule, LightningDataModule, seed_everything
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.plugins import DDPPlugin
from pytorch_lightning.metrics import Accuracy
from torch.utils.data  import random_split, DataLoader
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize

class ContrastiveUnsupervisedDataset(torch.utils.data.Dataset):
    """
    This class takes a dataset and creates a contrastive version of that dataset.
    Each item of the dataset is a tuple of a clean image and a noisy image (two
    separate transformations.)
    """
    def __init__(self, clean_dataset, transform_contrastive=None, return_label=False):
        self.base = clean_dataset
        self.transform_contrastive = transform_contrastive
        self.return_label = return_label

    def __len__(self):
        return len(self.base)

    def __getitem__(self, idx):
        image_orig, label = self.base[idx]
        image_clean, image_noisy = self.transform_contrastive(image_orig) if self.transform_contrastive is not None else (image_orig, image_orig)
        if self.return_label:
            return image_clean, image_noisy, label
        else:
            return image_clean, image_noisy

class ImageNetCLIPDataset(LightningDataModule):
    """
    Wrapper class for the ImageNet dataset, handles all data manipulations
    required in order to train the NoisyCLIP model.
    """
    def __init__(self, args):
        super(ImageNetCLIPDataset, self).__init__()

        self.hparams = args

        self.dataset_dir = self.hparams.dataset_dir
        self.batch_size = self.hparams.batch_size

        if self.hparams.distortion == "None":
            self.train_set_transform = ImageNetBaseTrainContrastive(self.hparams)
            self.val_set_transform = ImageNetBaseTransformVal(self.hparams)
        else:
            #set up the training transform and if we want a fixed mask, transfer the same mask to the validation transform
            self.train_set_transform = ImageNetDistortTrainContrastive(self.hparams)

            elif self.hparams.fixed_mask:
                self.val_set_transform = ImageNetDistortVal(self.hparams, fixed_distortion=self.train_set_transform.distortion)
            else:
                self.val_set_transform = ImageNetDistortVal(self.hparams)

    def setup(self, stage=None):
        train_data = ImageNet100(
        	root=self.hparams.dataset_dir,
            split="train",
            transform=None
        )
        self.val_data = ImageNet100(
            root=self.hparams.dataset_dir,
            split="val",
            transform=self.val_set_transform
        )

        filename = self.hparams.dataset_dir + self.hparams.subset_file_name

        # Get the subset, as well as its labels as text.
        text_labels = list(train_data.idx_to_class.values())

        self.train_contrastive = ContrastiveUnsupervisedDataset(train_data, transform_contrastive=self.train_set_transform, return_label=True)

        # Save labels to be reused.
        if self.hparams.save_mapping_and_text:
            pickle.dump(text_labels, open(self.hparams.mapping_and_text_file, 'wb'))

    def train_dataloader(self):
        return DataLoader(self.train_contrastive, batch_size=self.batch_size, num_workers=self.hparams.workers, pin_memory=True, shuffle=True)

    def val_dataloader(self):
        return DataLoader(self.val_data, batch_size=2*self.batch_size, num_workers=self.hparams.workers, pin_memory=True, shuffle=False)


class NoisyCLIP(LightningModule):
    def __init__(self, args):
        """
        A class that trains OpenAI CLIP in a student-teacher fashion to classify distorted images.

        Given two identical pre-trained networks, Teacher - T() and Student S(), we freeze T() and train S().

        Given a batch of {x1, x2, ..., xN}, apply a given distortion to each one to obtain noisy images {y1, y2, ..., yN}.

        Feed original images to T() and obtain embeddings {T(x1), ..., T(xN)} and feed distorted images to S() and obtain embeddings {S(y1), ..., S(yN)}.

        Maximize the similarity between the pairs {(T(x1), S(y1)), ..., (T(xN), S(yN))} while minimizing the similarity between all non-matched pairs. 
        """
        super(NoisyCLIP, self).__init__()
        self.hparams = args
        self.world_size = self.hparams.num_nodes * self.hparams.gpus

        #(1) Load the correct dataset class names
        if self.hparams.dataset == "Imagenet-100":
            self.N_val = 5000 # Default ImageNet validation set, only 100 classes.

            #we need a file which gives us the text labels for classes in order to do zero-shot classification!
            if self.hparams.mapping_and_text_file is None:
                raise ValueError('No file from which to read text labels was specified.')

            text_labels = pickle.load(open(self.hparams.mapping_and_text_file, 'rb'))
            self.text_list = ['A photo of '+label.strip().replace('_',' ') for label in text_labels] #list of text labels for our classes, each called "a photo of [class name]"
        else:
            raise NotImplementedError('Handling of the dataset not implemented yet.')

        #(2) set up the teacher CLIP network - freze it and don't use gradients!
        self.baseclip = clip.load(self.hparams.baseclip_type, self.hparams.device, jit=False)[0]
        self.baseclip.eval()
        self.baseclip.requires_grad_(False)

        with torch.no_grad():
            self.text_features = self.baseclip.encode_text(clip.tokenize(self.text_list)) #we pre-calculate the text features for every class prompt!

        #(3) set up the student CLIP network - unfreeze it and use gradients!
        self.noisy_visual_encoder = clip.load(self.hparams.baseclip_type, self.hparams.device, jit=False)[0].visual
        self.noisy_visual_encoder.train()
        self.noisy_visual_encoder.requires_grad_(True)

        #(4) set up the training and validation losses!
        self.logit_scale = self.hparams.logit_scale #Tau in the InfoNCE loss - temperature

        self.val_top_1 = Accuracy(top_k=1)
        self.val_top_5 = Accuracy(top_k=5)

    def criterion(self, input1, input2, reduction='mean'):
        """
        Args:
            input1: Embeddings of the clean/noisy images from the teacher/student. Size [N, embedding_dim].
            input2: Embeddings of the clean/noisy images from the teacher/student (the ones not used as input1). Size [N, embedding_dim].
            reduction: how to scale the final loss
        """
        bsz = input1.shape[0]

        #Use the simclr style InfoNCE
        if self.hparams.loss_type == 'simclr':
            # Create similarity matrix between embeddings.
            full_tensor = torch.cat([input1.unsqueeze(1),input2.unsqueeze(1)], dim=1).view(2*bsz, 1, -1)
            tensor1 = full_tensor.expand(2*bsz,2*bsz,-1)
            tensor2 = full_tensor.permute(1,0,2).expand(2*bsz,2*bsz,-1)
            sim_mat = torch.nn.CosineSimilarity(dim=-1)(tensor1,tensor2)

            # Calculate logits used for the contrastive loss.
            exp_sim_mat = torch.exp(sim_mat/self.hparams.loss_tau)
            mask = torch.ones_like(exp_sim_mat) - torch.eye(2*bsz).type_as(exp_sim_mat)
            logmat = -torch.log(exp_sim_mat)+torch.log(torch.sum(mask*exp_sim_mat, 1))
            
            #Grab the two off-diagonal similarities 
            part1 = torch.sum(torch.diag(logmat, diagonal=1)[np.arange(0,2*bsz,2)])
            part2 = torch.sum(torch.diag(logmat, diagonal=-1)[np.arange(0,2*bsz,2)])

            #Take the mean of the two off-diagonals 
            loss = (part1 + part2)/2

        #Use the CLIP-style InfoNCE
        elif self.hparams.loss_type == 'clip':
            # Create similarity matrix between embeddings.
            tensor1 = input1 / input1.norm(dim=-1, keepdim=True)
            tensor2 = input2 / input2.norm(dim=-1, keepdim=True)
            sim_mat = (1/self.hparams.loss_tau)*tensor1 @ tensor2.t()

            #Calculate the cross entropy between the similarities of the positive pairs, counted two ways
            part1 = F.cross_entropy(sim_mat, torch.LongTensor(np.arange(bsz)).type_as(input1))
            part2 = F.cross_entropy(sim_mat.t(), torch.LongTensor(np.arange(bsz)).type_as(input1))

            #Take the mean of the two off-diagonals 
            loss = (part1+part2)/2

        #Take the simple MSE between the clean and noisy embeddings
        elif self.hparams.loss_type == 'mse':
            return F.mse_loss(input2, input1)

        else:
            raise ValueError('Loss function not understood.')

        return loss/bsz if reduction == 'mean' else loss


    def configure_optimizers(self):
        opt = torch.optim.Adam(self.noisy_visual_encoder.parameters(), lr = self.hparams.lr)

        num_steps = 126689//(self.hparams.batch_size * self.hparams.gpus) #divide N_train by number of distributed iters

        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=num_steps)

        return [opt], [scheduler]

    def encode_noisy_image(self, image):
        """
        Return S(yi) where S() is the student network and yi is distorted images.
        """

        return self.noisy_visual_encoder(image.type(torch.float16))

    def forward(self, image_features):
        """
        Given a set of noisy image embeddings, calculate the cosine similarity (scaled by temperature) of each image with each class text prompt.
        Calculates the similarity in two ways: logits per image (size = [N, n_classes]), logits per text (size = [n_classes, N]).  
        This is mainly used for validation and classification.

        Args:
            image_features: the noisy image embeddings S(yi) where S() is the student and yi = Distort(xi). Shape [N, embedding_dim]
        """
        #load the pre-computed text features and load them on the correct device
        text_features = self.text_features.type_as(image_features)

        # normalized features
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        # cosine similarity as logits
        logits_per_image = self.logit_scale * image_features.type(torch.float16) @ text_features.type(torch.float16).t() # Funny thing, here the original code spells 'iamge' instead of image. Hidden copyright protection? :p
        logits_per_text = self.logit_scale * text_features.type(torch.float16) @ image_features.type(torch.float16).t()

        return logits_per_image, logits_per_text

    # Training methods - here we are concerned with contrastive loss (or MSE) between clean and noisy image embeddings. 
    def training_step(self, train_batch, batch_idx):
        """
        Takes a batch of clean and noisy images and returns their respective embeddings.
        
        Returns:
            embed_clean: T(xi) where T() is the teacher and xi are clean images. Shape [N, embed_dim]
            embed_noisy: S(yi) where S() is the student and yi are noisy images. Shape [N, embed_dim]
        """
        image_clean, image_noisy, _ = train_batch

        self.baseclip.eval()
        with torch.no_grad():
            embed_clean = self.baseclip.encode_image(image_clean)

        embed_noisy = self.encode_noisy_image(image_noisy)
        
        return {'embed_clean': embed_clean, 'embed_noisy': embed_noisy}
        
    def training_step_end(self, outputs):
        """
        Given all the clean and noisy image embeddings form across GPUs from training_step, gather them onto a single GPU and calculate overall loss. 
        """
        embed_clean_full = outputs['embed_clean']
        embed_noisy_full = outputs['embed_noisy']

        loss = self.criterion(embed_clean_full, embed_noisy_full)

        return loss

    # Validation methods - here we are concerned with similarity between noisy image embeddings and classification text embeddings.
    def validation_step(self, test_batch, batch_idx):
        """
        Grab the noisy image embeddings: S(yi), where S() is the student and yi = Distort(xi). Done on each GPU.
        Return these to be evaluated in validation step end.
        """
        images_noisy, labels = test_batch

        if batch_idx == 0 and self.current_epoch < 1:
            self.logger.experiment.add_image('Val_Sample', img_grid(images_noisy), self.current_epoch)

        image_features = self.encode_noisy_image(images_noisy)

        return {'image_features': image_features, 'labels': labels}

    def validation_step_end(self, outputs):
        """
        Gather the noisy image features and their labels from each GPU.
        Then calculate their similarities, convert to probabilities, and calculate accuracy on each GPU.
        """
        image_features_full = outputs['image_features'] #[N, embed_dim]
        labels_full = outputs['labels'] #[n_classes]

        image_logits, _ = self.forward(image_features_full) #shape [N, n_classes]
        image_logits = image_logits.float() #convert back to floating point for precision
        image_probs = image_logits.softmax(dim=-1) #convert logits to probabilities

        self.log('val_top_1_step', self.val_top_1(image_probs, labels_full), prog_bar=False, logger=False)
        self.log('val_top_5_step', self.val_top_5(image_probs, labels_full), prog_bar=False, logger=False)


    def validation_epoch_end(self, outputs):
        """
        Gather the zero-shot validation accuracies from across GPUs and reduce. 
        """
        self.log('val_top_1', self.val_top_1.compute(), prog_bar=True, logger=True)
        self.log('val_top_5', self.val_top_5.compute(), prog_bar=True, logger=True)

def run_noisy_clip():
    args = grab_config()

    seed_everything(args.seed)

    dataset = ImageNetCLIPDataset(args)
    dataset.setup()
    model = NoisyCLIP(args)

    logger = TensorBoardLogger(
        save_dir=args.logdir,
        version=args.experiment_name,
        name='NoisyCLIP_Logs'
    )
    trainer = Trainer.from_argparse_args(args, logger=logger)

    trainer.fit(model, dataset)

def grab_config():
    parser = argparse.ArgumentParser(description="NoisyCLIP")

    parser.add_argument('--config_file')

    config = yaml_config_hook(parser.parse_args().config_file)
    for k, v in config.items():
        parser.add_argument(f"--{k}", default=v, type=type(v))

    args = parser.parse_args()

    return args

if __name__ == "__main__":
    run_noisy_clip()