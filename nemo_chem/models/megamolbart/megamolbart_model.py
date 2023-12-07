# Copyright (c) 2022, NVIDIA CORPORATION.
# SPDX-License-Identifier: Apache-2.0

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from omegaconf.dictconfig import DictConfig
from rdkit import Chem

import torch
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.language_modeling.megatron_lm_encoder_decoder_model import MegatronLMEncoderDecoderModel
from nemo.utils import logging
from nemo.collections.nlp.modules.common.megatron.utils import average_losses_across_data_parallel_group

from nemo_chem.utils import flatten_dict
from nemo_chem.data import DatasetTypes, MoleculeEnumeration, build_train_valid_test_datasets, PrepareDataset


# Disable logging of invalid SMILES moloecules
from rdkit import RDLogger
lg = RDLogger.logger()
lg.setLevel(RDLogger.CRITICAL)

__all__ = ["MegaMolBARTModel"]

class MegaMolBARTModel(MegatronLMEncoderDecoderModel):
    """
    MegaMolBART pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        self._check_scheduler(cfg)
        super().__init__(cfg, trainer=trainer)

    def _check_scheduler(self, cfg):
        """Warn if maximum learning rate with Noam is less than minimum learning rate"""
        # TODO add to Noam Scheduler in NeMo
        if cfg.optim.sched.name == 'NoamAnnealing':
            if cfg.optim.sched.warmup_steps:
                warmup_steps = cfg.optim.sched.warmup_steps
            else:
                warmup_steps = int(cfg.optim.sched.warmup_ratio * cfg.optim.sched.max_steps)
            max_lr = cfg.optim.lr * cfg.optim.sched.d_model**(-0.5) * warmup_steps**(-0.5)
            min_lr = cfg.optim.sched.min_lr
            if max_lr <= min_lr:
                logging.warning(f'Warning: maximum learning rate for Noam Scheduler ({max_lr}) is less than minimum ({min_lr}).')
        return

    def build_train_valid_test_datasets(self):
        logging.info('Building MegaMolBART datasets.')
        tensor_model_parallel_size = self._cfg.get('tensor_model_parallel_size', 1)

        global_batch_size = self.trainer.world_size * self._cfg.micro_batch_size / tensor_model_parallel_size
        eval_iters = (self.trainer.max_steps // self.trainer.val_check_interval + 1) * self.trainer.limit_val_batches
        test_iters = self.trainer.limit_test_batches
        train_valid_test_num_samples = [
            int(self.trainer.max_steps * global_batch_size),
            int(eval_iters * global_batch_size),
            int(test_iters * global_batch_size),
        ]

        if self._cfg.data.get('dataset_type', None) is not None:
            dataset_types = DatasetTypes.__members__
            if self._cfg.data.get('dataset_type') not in dataset_types:
                raise ValueError(f"dataset_type must be in {dataset_types}. Found {self._cfg.data.get('dataset_type')}")

        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            self._cfg.data,
            self.trainer,
            train_valid_test_num_samples
        )

        logging.info(f'Length of train dataset: {len(self._train_ds)}')
        logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building MegaMolBART datasets.')
        return self._train_ds, self._validation_ds, self._test_ds

    def build_pretraining_data_loader(self, dataset, consumed_samples):
        """Buld dataloader given an input dataset."""

        assert self._cfg.data.dataloader_type == 'single', AssertionError(
            f'Only the Megatron sequential ("single") sampler is currently supported. {self._cfg.data.dataloader_type} was chosen.'
            )

        dataloader = super().build_pretraining_data_loader(dataset=dataset, consumed_samples=consumed_samples)

        # Add collate function and unpin memory to avoid crash with CUDA misaligned address
        dataloader.pin_memory = False # must be False with CSV dataset TODO check with binary
        pad_size_divisible_by_8 = True if self._cfg.masked_softmax_fusion else False

        ## TODO: We can use the Enum DatasetType() defined in the utils.py here.
        if self._cfg.data.dataset_format == "bin":
            dataloader.collate_fn = PrepareDataset(tokenizer=self.tokenizer,
                                                        seq_length=self._cfg.seq_length,
                                                        pad_size_divisible_by_8=pad_size_divisible_by_8,
                                                        **self._cfg.data).collate_fn
        elif self._cfg.data.dataset_format == "csv":
            dataloader.collate_fn = MoleculeEnumeration(tokenizer=self.tokenizer,
                                                        seq_length=self._cfg.seq_length,
                                                        pad_size_divisible_by_8=pad_size_divisible_by_8,
                                                        **self._cfg.data).collate_fn

        return dataloader

    def process_global_batch(self, global_batch):
        # FIXME: move to device correctly
        # FIXME: move to precission correctly (fails with 16)
        tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask =  (
            global_batch["text_enc"],
            global_batch["text_dec"],
            global_batch["loss_mask"],
            global_batch["labels"],
            global_batch["enc_mask"],
            global_batch["dec_mask"],
        )

        device = next(self.parameters()).device
        tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask = [t.to(device) for t in (tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask)]

        return (tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask)

    def _inference_epoch_end(self, outputs, mode):
        results_dict = flatten_dict(outputs)

        # Calculate metric averages
        # TODO this reduces all metrics across all data parallel groups
        # if too slow, can only reduce loss instead
        averaged_results = {}
        for metric_name, metric_list in results_dict.items():
            reduced_metric = average_losses_across_data_parallel_group(metric_list)
            logged_name = 'reduced_loss' if metric_name == 'loss' else metric_name
            averaged_results[logged_name] = reduced_metric.cpu().detach().numpy().mean()

        # Log results
        log_list = []
        for metric_name, metric_val in averaged_results.items():
            metric_name = metric_name.replace('_', ' ').title()
            log_list.append(f'{metric_name}: {metric_val:.2f}')
        logging.info(f'{mode.title()} Results: ' + ', '.join(log_list))

        # Prepend val/test tag to metric for Tensorboard / WandB
        logged_results = {}
        for metric_name, metric_val in averaged_results.items():
            logged_results[f'{mode}_{metric_name}'] = metric_val

        self.log_dict(logged_results, prog_bar=True)

    def validation_step(self, batch, batch_idx):
        loss_mean = super().validation_step(batch, batch_idx)
        token_logits = self.validation_step_logits(batch, batch_idx)
        tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask = \
            self.process_global_batch(batch)

        target_smiles = batch['target_smiles']
        token_logits[:, :, self.tokenizer.vocab_size:] = -float('Inf') # never pick padded tokens

        log_n_batches=10
        log_mol = True if batch_idx < log_n_batches else False # TODO enable logging in yaml config
        metrics = self.calculate_metrics(token_logits=token_logits,
                                         loss_mask=loss_mask,
                                         labels=labels,
                                         tokens_enc=tokens_enc,
                                         enc_mask=enc_mask,
                                         target_smiles=target_smiles,
                                         batch_idx=batch_idx,
                                         log_char=False,
                                         log_mol=log_mol)

        logs = {'loss': loss_mean}
        for metric_name, metric_value in metrics.items():
            logs[metric_name] = metric_value

        # return loss_mean
        return logs

    def validation_epoch_end(self, outputs):
        if len(outputs) == 0:
            return

        logging.info('Finishing validation epoch')
        all_keys = list(outputs[0].keys())
        new_outputs = {}
        for k in all_keys:
            new_outputs[k] = super().validation_epoch_end([o[k] for o in outputs])

        self._inference_epoch_end(outputs, mode='val')

    def test_epoch_end(self, outputs):
        logging.info('Finishing test epoch')
        super().test_epoch_end(outputs)
        self._inference_epoch_end(outputs, mode='test')

    def sample_molecules(self, tokens_enc, enc_mask, hidden_states=None):
        """Autoregressively sample SMILES molecules from encoder hidden state

        Args:
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections

        Returns:
            sampled_smiles (list[str]): a list of sampled SMILES strings
        """

        self.freeze()

        # Decode encoder hidden state to tokens
        predicted_tokens_ids, log_probs = self.decode(tokens_enc,
                                                      enc_mask,
                                                      self._cfg.max_position_embeddings,
                                                      enc_output=hidden_states)
        predicted_tokens_ids = predicted_tokens_ids.cpu().detach().numpy().tolist()

        # Prune tokens by eos / padding and convert to SMILES
        for item, predicted_tokens_ in enumerate(predicted_tokens_ids):
            if self.tokenizer.eos_id in predicted_tokens_:
                idx = predicted_tokens_.index(self.tokenizer.eos_id)
                predicted_tokens_ids[item] = predicted_tokens_[:idx]
            else:
                # NB: this is slightly different from previous version in that pad tokens can be in the middle of sequence
                predicted_tokens_ids[item] = [id for id in predicted_tokens_ if id != self.tokenizer.pad_id]

        predicted_tokens_text = self.tokenizer.ids_to_tokens(predicted_tokens_ids)
        sampled_smiles = self.tokenizer.tokens_to_text(predicted_tokens_text)

        self.unfreeze()
        return sampled_smiles

    def calculate_character_accuracy(self, token_logits, loss_mask, labels, batch_idx=None, log=False):
        """Character (token) level accuracy

        Args:
            token_logits (torch.Tensor, float): softmax values for all tokens
            loss_mask (torch.Tensor, float): binary mask for ignored data (1=active, 0=mask), must be float
            labels (torch.Tensor, long): token IDs for correct output

        Returns:
            float: character accuracy value
        """

        # Get most probable token
        _, predicted_tokens = torch.max(token_logits, dim=2)
        correct_tokens = torch.eq(labels, predicted_tokens) * loss_mask # NB: mask includes EOS in calculation

        # Calculate percent of correct tokens
        num_correct = correct_tokens.detach().sum()
        total = loss_mask.detach().sum()
        character_accuracy = num_correct / total

        if log:
            logging.info(f'Character accuracy for batch {batch_idx}:')
            for idx in range(predicted_tokens.shape[0]):
                mask = loss_mask[idx].to(int)
                correct_ = labels[idx][mask] == predicted_tokens[idx][mask]
                logging.info(f'     Sample {idx} has {correct_} / {sum(mask)}')

        return character_accuracy

    def calculate_molecular_accuracy(self, tokens_enc, enc_mask, target_smiles, batch_idx=None, log=False):
        """Calculate molecular accuracy (with canonicalization)

        Args:
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections
            target_smiles (str): ground truth for canonicalized SMILES

        Returns:
            float, float: molecular accuracy and percent invalid
        """
        sampled_smiles = self.sample_molecules(tokens_enc, enc_mask)
        sampled_mols = [Chem.MolFromSmiles(smi) for smi in sampled_smiles]

        invalid = [mol is None for mol in sampled_mols]
        canonical_smiles = ["Unknown" if mol is None else Chem.MolToSmiles(mol, canonical=True) for mol in sampled_mols]
        correct_smiles = [target_smiles[idx] == smi for idx, smi in enumerate(canonical_smiles)]

        num_correct = sum(correct_smiles)
        total = len(correct_smiles)
        num_invalid = sum(invalid)
        percent_invalid = torch.tensor([num_invalid / total]).to(tokens_enc.device)
        molecular_accuracy = torch.tensor([num_correct / total]).to(tokens_enc.device)

        if log:
            logging.info(f'Molecular accuracy for batch {batch_idx}:')
            for idx, (invalid_, correct_) in enumerate(zip(invalid, correct_smiles)):
                if invalid_:
                    result = 'invalid'
                elif correct_:
                    result = 'correct'
                else:
                    result = 'incorrect'
                logging.info(f'     Sample {idx} is {result}, target: {target_smiles[idx]}, sample: {sampled_smiles[idx]}')

        return molecular_accuracy, percent_invalid

    def calculate_metrics(self, token_logits, loss_mask, labels, tokens_enc, enc_mask, target_smiles, batch_idx=None, log_char=False, log_mol=False):
        """Calculate metrics for character accuracy, molecular accuracy, and invalid molecules

        Args:
            token_logits (torch.Tensor, float): softmax values for all tokens
            loss_mask (torch.Tensor, float): binary mask for ignored data (1=active, 0=mask), must be float
            labels (torch.Tensor, long): token IDs for correct output
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections
            target_smiles (str): ground truth for canonicalized SMILES

        Returns:
            dict: dictionary of metric values
        """
        character_accuracy = self.calculate_character_accuracy(token_logits, loss_mask, labels, batch_idx, log=log_char)
        molecular_accuracy, percent_invalid = self.calculate_molecular_accuracy(tokens_enc, enc_mask, target_smiles, batch_idx, log=log_mol)
        metrics = {'character_accuracy': character_accuracy,
                   'molecular_accuracy': molecular_accuracy,
                   'percent_invalid': percent_invalid}
        return metrics

    def list_available_models(self):
        pass
