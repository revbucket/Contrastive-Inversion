#!/usr/bin/env python

import numpy as np
import os
import sys
import time
import torch
import torch.nn.functional as F

from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV
from torch.utils.data import DataLoader
from torchvision.datasets import CIFAR10

import clip
from utils import RandomMask, SquareMask
from noisy_clip import ContrastiveUnsupervisedDataset, ModifiedCLIP
from tqdm import tqdm
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
from PIL import Image


def main(debug=True, data="CIFAR10"):
    # Keep these guys here for cmd arguments eventually
    # debug = None
    # if len(args) > 1:
    #     debug = args[1]
    # if debug == "True" or debug == "true" or debug == "y" or debug == "Y":
    #     debug = True
    #     print("Debug: True")
    # else:
    #     debug = False
    #     print("Debug: False")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    # device = 'cpu'

    if device=="cuda":
        torch.cuda.empty_cache()

    root = os.path.expanduser("~/.cache")

    batch_size = 8

    # models = ['RN50', 'ViT-B/32']
    models = ['ViT-B/32']

    # trans_types = ["None", "Random", "Blur", "Square"]
    trans_types = ['Random']

    # pct_missings = [0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.3, 0.5]
    pct_missings = [0.01]

    sq_lens = [10, 20, 30, 50, 75, 100, 150, 200]
    # sq_lens = [20]

    blur_size = 11
    blur_stds = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20]
    #blur_stds = [0.1]

    for m in models:
        if debug:
           print("LOADING MODEL: " + m)
           start = time.time()

        baseclip, train_preprocess = clip.load(m, device)
        n_px = baseclip.input_resolution.item()

        if debug:
           print("\nELAPSED TIME: ", str(time.time() - start))

        if debug:
            print("\nPERFORMING CROSS-VALIDATION TO FIND BEST LOGISTIC REGRESSION MODEL")
            start = time.time()

        if debug:
           print("\nELAPSED TIME: ", str(time.time() - start))

        for trans in trans_types:

            name_str = m
            if name_str == 'ViT-B/32':
                name_str = 'ViT'
            name_str += trans

            if trans=="Random":
                if debug:
                    print("\n====Random Masking====")

                accuracies = []

                for pct in pct_missings:
                    if debug:
                        print("PCT MISSING: ", str(pct))
                        start = time.time()

                    train_base = CIFAR10(root, download=True, train=True, transform=None)
                    train_contrastive = ContrastiveUnsupervisedDataset(train_base, transform_clean=train_preprocess, transform_noisy=Compose([train_preprocess, RandomMask(pct)]))
                    train_dl = DataLoader(train_contrastive, batch_size=batch_size, shuffle=True)

                    new_model = ModifiedCLIP(baseclip, device=device)
                    new_model.fit(train_dl)

                    test_dataset = CIFAR10(root, download=True, train=False, transform=Compose([train_preprocess, RandomMask(pct)]))
                    CIFAR10_labels = ['airplane', 'automobile', 'bird', 'cat', 'deer', 'dog', 'frog','horse','ship','truck']
                    test_dl = DataLoader(test_dataset, batch_size=batch_size)
                    acc = new_model.score(test_dl, CIFAR10_labels)

                    accuracies.append(acc)

                    if debug:
                        print("\nELAPSED TIME: ", str(time.time() - start))

                if not os.path.exists(os.path.join(os.getcwd(),'/results_noisy/')):
                    os.mkdir(os.path.join(os.getcwd(),'/results_noisy/'))
                if not os.path.exists(os.path.join(os.getcwd(),'/results_noisy/', data)):
                    os.mkdir(os.path.join(os.getcwd(),'/results_noisy/', data))
                np.savetxt(os.path.join(os.getcwd(),'/results_noisy/', data, name_str), np.array(accuracies))

            elif trans=="Square":
                raise NotImplementedError()

            elif trans=="Blur":
                raise NotImplementedError()

    return

if __name__ == "__main__":
    main()
