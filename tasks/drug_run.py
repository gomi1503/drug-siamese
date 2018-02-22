import sys
import pickle
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import logging
import csv
import os

from datetime import datetime
from torch.autograd import Variable
from models.root.utils import *


LOGGER = logging.getLogger(__name__)


def run_drug(model, loader, dataset, args, metric, train=False):
    total_step = 0.0
    stats = {'loss':[]}
    tar_set = []
    pred_set = []
    kk_tar_set = []
    kk_pred_set = []
    ku_tar_set = []
    ku_pred_set = []
    uu_tar_set = []
    uu_pred_set = []
    start_time = datetime.now()

    for d_idx, (d1, d1_r, d1_l, d2, d2_r, d2_l, score) in enumerate(loader):

        # Split for KK/KU/UU sets
        kk_idx = np.argwhere([a in dataset.known and b in dataset.known
                              for a, b in zip(d1, d2)]).flatten()
        ku_idx = np.argwhere([(a in dataset.unknown) != (b in dataset.unknown)
                              for a, b in zip(d1, d2)]).flatten()
        uu_idx = np.argwhere([a in dataset.unknown and b in dataset.unknown
                              for a, b in zip(d1, d2)]).flatten()
        assert len(kk_idx) + len(ku_idx) + len(uu_idx) == len(d1)

        # Grad zero + mode change
        model.optimizer.zero_grad()
        if train: model.train(train)
        else: model.eval()

        # Get outputs
        outputs, embed1, embed2 = model(d1_r.cuda(), d1_l, d2_r.cuda(), d2_l)
        loss = model.get_loss(outputs, score.cuda())
        stats['loss'] += [loss.data[0]]
        total_step += 1.0

        # Metrics for binary classification
        tmp_tar = score.data.cpu().numpy()
        tmp_pred = outputs.data.cpu().numpy()
        if args.binary:
            tmp_pred = np.array([float(p > 0.5) for p in tmp_pred[:]])

        # Accumulate for final evaluation
        tar_set += list(tmp_tar[:])
        pred_set += list(tmp_pred[:])
        kk_tar_set += list(tmp_tar[kk_idx])
        kk_pred_set += list(tmp_pred[kk_idx])
        ku_tar_set += list(tmp_tar[ku_idx])
        ku_pred_set += list(tmp_pred[ku_idx])
        uu_tar_set += list(tmp_tar[uu_idx])
        uu_pred_set += list(tmp_pred[uu_idx])
    
        # Calculate current f1 scores
        f1 = metric(list(tmp_tar[:]), list(tmp_pred[:]))
        f1_kk = metric(list(tmp_tar[kk_idx]), list(tmp_pred[kk_idx]))
        f1_ku = metric(list(tmp_tar[ku_idx]), list(tmp_pred[ku_idx]))
        f1_uu = metric(list(tmp_tar[uu_idx]), list(tmp_pred[uu_idx]))

        # for regression
        if not args.binary:
            f1 = f1[0][1]
            f1_kk = f1_kk[0][1]
            f1_ku = f1_ku[0][1]
            f1_uu = f1_uu[0][1]

        # Optimize model
        if train and not args.save_embed:
            loss.backward()
            nn.utils.clip_grad_norm(model.get_model_params()[1], 
                    args.grad_max_norm)
            # for p in model.get_model_params()[1]:
            #     if p.grad is not None:
            #         p.grad.data.clamp_(-args.grad_clip, args.grad_clip)
            model.optimizer.step()
        
        # Print for print step or at last
        if d_idx % args.print_step == 0 or d_idx == (len(loader) - 1):
            et = int((datetime.now() - start_time).total_seconds())
            _progress = (
                '{}/{} | Loss: {:.3f} | Total F1: {:.3f} | '.format(
                d_idx + 1, len(loader), loss.data[0], f1) +
                'KK: {:.3f} KU: {:.3f} UU: {:.3f} | '.format(
                f1_kk, f1_ku, f1_uu) +
                '{:2d}:{:2d}:{:2d}'.format(
                et//3600, et%3600//60, et%60))
            LOGGER.info(_progress)

    # Calculate acuumulated f1 scores
    f1 = metric(tar_set, pred_set)
    f1_kk = metric(kk_tar_set, kk_pred_set)
    f1_ku = metric(ku_tar_set, ku_pred_set)
    f1_uu = metric(uu_tar_set, uu_pred_set)
    if not args.binary:
        f1 = f1[0][1]
        f1_kk = f1_kk[0][1]
        f1_ku = f1_ku[0][1]
        f1_uu = f1_uu[0][1]

    # TODO add spearman correlation

    # End of an epoch
    et = (datetime.now() - start_time).total_seconds()
    LOGGER.info('Results (Loss/F1/KK/KU/UU): {:.3f}\t{:.3f}\t'.format(
        sum(stats['loss'])/len(stats['loss']), f1) +
        '{:.3f}\t{:.3f}\t{:.3f}'.format(
        f1_kk, f1_ku, f1_uu))

    return f1_ku


# Outputs response embeddings for a given dictionary
def save_embed(model, dictionary, dataset, args, drug_file):
    model.eval()
    key2vec = {}
    known_cnt = 0

    # Iterate drug dictionary
    for idx, (drug, rep) in enumerate(dictionary.items()):
        d1_r = rep[0]
        d1_k = rep[1]
        d1_l = len(d1_r)

        # For string data (smiles/inchikey)
        if args.rep_idx == 0 or args.rep_idx == 1:
            d1_r = list(map(lambda x: dataset.char2idx[x]
                        if x in dataset.char2idx
                        else dataset.char2idx[dataset.UNK], d1_r))
            d1_l = len(d1_r)

        # Real valued for mol2vec
        if args.rep_idx != 3:
            d1_r = Variable(torch.LongTensor(d1_r)).cuda()
        else:
            d1_r = Variable(torch.FloatTensor(d1_r)).cuda()
        d1_l = torch.LongTensor(np.array([d1_l]))
        d1_r = d1_r.unsqueeze(0)
        d1_l = d1_l.unsqueeze(0)

        # Run model amd save embed
        _, embed1, embed2 = model(d1_r, d1_l, d1_r, d1_l)
        assert embed1.data.tolist() == embed2.data.tolist()
        """
        known = False
        for pert_id, _ in dataset.drugs.items():
            if drug == pert_id:
                known = True
                known_cnt += 1
                break
        """
        key2vec[drug] = [embed1.squeeze().data.tolist(), d1_k]

        # Print progress
        if idx % args.print_step == 0 or idx == len(dictionary) - 1:
            _progress = '{}/{} saving drug embeddings..'.format(
                idx + 1, len(dictionary))
            LOGGER.info(_progress)

    # Save embed as pickle
    pickle.dump(key2vec, open('{}/embed/{}.{}.pkl'.format(
                args.checkpoint_dir, drug_file, args.model_name), 'wb'), 
                protocol=2)
    LOGGER.info('{}/{} number of known drugs.'.format(known_cnt, len(key2vec)))


# Outputs pred vs label scores given a dataloader
def save_prediction(model, loader, dataset, args):
    model.eval()
    csv_writer = csv.writer(open(args.checkpoint_dir + 'pred_' + 
                                 args.model_name + '.csv', 'w'))
    csv_writer.writerow(['pert1', 'pert1_known', 'pert2', 'pert2_known',
                         'prediction', 'target'])

    for d_idx, (d1, d1_r, d1_l, d2, d2_r, d2_l, score) in enumerate(loader):

        # Run model for getting predictions
        outputs, _, _ = model(d1_r.cuda(), d1_l, d2_r.cuda(), d2_l)
        predictions = outputs.data.cpu().numpy()
        targets = score.data.tolist()

        for a1, a2, a3, a4 in zip(d1, d2, predictions, targets):
            csv_writer.writerow([a1, a1 in dataset.known, 
                                 a2, a2 in dataset.known, a3, a4])

        # Print progress
        if d_idx % args.print_step == 0 or d_idx == len(loader) - 1:
            _progress = '{}/{} saving drug predictions..'.format(
                d_idx + 1, len(loader))
            LOGGER.info(_progress)


# Outputs pred scores for new pair dataset
def save_pair_score(model, pair_dir, dataset, args):
    model.eval()

    # Iterate directory
    for subdir, _, files in os.walk(pair_dir):
        start = False
        for file_ in sorted(files):
            LOGGER.info('processing {}..'.format(file_))
            """
            if file_ != 'BGAB.csv' and start is False:
                continue
            else:
                start = True
            """

            with open(os.path.join(subdir, file_)) as f:

                # Ready for reader and writer
                csv_reader = csv.reader(f)
                csv_writer = csv.writer(open(args.checkpoint_dir + 'pairs/' +
                                             file_ + '.' + args.model_name + 
                                             '.pred', 'w'))
                csv_writer.writerow(['pert1', 'pert1_known', 
                                     'pert2', 'pert2_known',
                                     'prediction'])
                # For stats
                stats = {'kk': 0, 'ku': 0, 'uu': 0}
                batch = []
                kk_info = []

                # Iterate reader
                for row_idx, row in enumerate(csv_reader):
                    if row_idx == 0:
                        # print(row)
                        continue

                    # Assume each representation is smiles.
                    assert args.rep_idx == 0
                    rep1, rep2 = row

                    # Check if drug is known
                    rep1_k = next((reps for _, reps in dataset.drugs.items()
                                  if reps[0] == rep1), None) != None
                    rep2_k = next((reps for _, reps in dataset.drugs.items()
                                  if reps[0] == rep2), None) != None
                    if rep1_k and rep2_k:
                        stats['kk'] += 1
                    elif rep1_k != rep2_k:
                        stats['ku'] += 1
                    else:
                        stats['uu'] += 1

                    # Gather examples
                    example = ['pert1', rep1, len(rep1), 
                               'pert2', rep2, len(rep2), 0]
                    batch.append(example)
                    kk_info.append([rep1_k, rep2_k])

                    # Run model for each batch
                    if len(batch) == 128:
                        inputs = dataset.collate_fn(batch)

                        # Run model
                        output, _, _ = model(inputs[1].cuda(), inputs[2],
                                             inputs[4].cuda(), inputs[5])
                        pred = output.data.tolist()
                    
                        assert len(batch) == len(kk_info) == len(pred)
                        for ex, is_kk, p in zip(batch, kk_info, pred):
                            csv_writer.writerow([ex[1], is_kk[0], 
                                                 ex[4], is_kk[1], p])
                        batch = []
                        kk_info = []

                # Remaining batches
                if len(batch) > 0:
                    inputs = dataset.collate_fn(batch)
                    output, _, _ = model(inputs[1].cuda(), inputs[2],
                                         inputs[4].cuda(), inputs[5])
                    pred = output.data.tolist()
                    assert len(batch) == len(kk_info) == len(pred)
                    for ex, is_kk, p in zip(batch, kk_info, pred):
                        csv_writer.writerow([ex[1], is_kk[0], 
                                             ex[4], is_kk[1], p])

                LOGGER.info('kk/ku/uu stats: {}..'.format(sorted(stats.items())))
