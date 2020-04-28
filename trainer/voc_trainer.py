import time
import numpy as np
from typing import Tuple
import os
import torch
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from trainer.common import Averager, TTSSession, VocSession
from utils import hparams as hp
from utils.checkpoints import save_checkpoint
from utils.dataset import get_tts_datasets, get_vocoder_datasets
from utils.decorators import ignore_exception
from utils.display import stream, simple_table, plot_mel, plot_attention
from utils.distribution import discretized_mix_logistic_loss
from utils.dsp import reconstruct_waveform, rescale_mel, np_now, decode_mu_law, label_2_float, raw_melspec


class VocTrainer:

    def __init__(self, paths):
        self.paths = paths
        self.writer = SummaryWriter(log_dir=paths.voc_log, comment='v1')
        self.loss_func = F.cross_entropy if hp.voc_mode == 'RAW' else discretized_mix_logistic_loss
        self.top_k_models = []

    def train(self, model, optimizer, train_gta=False):
        for i, session_params in enumerate(hp.voc_schedule, 1):
            lr, max_step, bs = session_params
            if model.get_step() < max_step:
                train_set, val_set, val_set_samples = get_vocoder_datasets(
                    path=self.paths.data, batch_size=bs, train_gta=train_gta)
                session = VocSession(
                    index=i, lr=lr, max_step=max_step,
                    bs=bs, train_set=train_set, val_set=val_set,
                    val_set_samples=val_set_samples)
                self.train_session(model, optimizer, session, train_gta)

    def train_session(self, model, optimizer, session, train_gta):
        current_step = model.get_step()
        training_steps = session.max_step - current_step
        total_iters = len(session.train_set)
        epochs = training_steps // total_iters + 1
        simple_table([(f'Steps ', str(training_steps // 1000) + 'k'),
                      ('Batch Size', session.bs),
                      ('Learning Rate', session.lr),
                      ('Sequence Length', hp.voc_seq_len),
                      ('GTA Training', train_gta)])
        for g in optimizer.param_groups:
            g['lr'] = session.lr

        loss_avg = Averager()
        duration_avg = Averager()
        device = next(model.parameters()).device  # use same device as model parameters

        for e in range(1, epochs + 1):
            for i, (x, y, m) in enumerate(session.train_set, 1):
                start = time.time()
                model.train()
                x, m, y = x.to(device), m.to(device), y.to(device)

                y_hat = model(x, m)
                if model.mode == 'RAW':
                    y_hat = y_hat.transpose(1, 2).unsqueeze(-1)
                elif model.mode == 'MOL':
                    y = y.float()
                y = y.unsqueeze(-1)

                loss = self.loss_func(y_hat, y)
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), hp.voc_clip_grad_norm)
                optimizer.step()
                loss_avg.add(loss.item())
                step = model.get_step()
                k = step // 1000

                duration_avg.add(time.time() - start)
                speed = 1. / duration_avg.get()
                msg = f'| Epoch: {e}/{epochs} ({i}/{total_iters}) | Loss: {loss_avg.get():#.4} ' \
                      f'| {speed:#.2} steps/s | Step: {k}k | '

                if step % hp.voc_gen_samples_every == 0:
                    mel_loss, gen_wav = self.generate_samples(model, session)
                    self.writer.add_scalar('Loss/val_mel_l1', mel_loss, model.get_step())
                    self.save_top_models(mel_loss, gen_wav, model)

                if step % hp.voc_checkpoint_every == 0:
                    ckpt_name = f'wave_step{k}K'
                    save_checkpoint('voc', self.paths, model, optimizer,
                                    name=ckpt_name, is_silent=True)

                self.writer.add_scalar('Loss/train', loss, model.get_step())
                self.writer.add_scalar('Params/batch_size', session.bs, model.get_step())
                self.writer.add_scalar('Params/learning_rate', session.lr, model.get_step())

                stream(msg)

            val_loss = self.evaluate(model, session.val_set)
            self.writer.add_scalar('Loss/val', val_loss, model.get_step())
            save_checkpoint('voc', self.paths, model, optimizer, is_silent=True)

            loss_avg.reset()
            duration_avg.reset()
            print(' ')

    def evaluate(self, model, val_set) -> float:
        model.eval()
        val_loss = 0
        device = next(model.parameters()).device
        for i, (x, y, m) in enumerate(val_set, 1):
            x, m, y = x.to(device), m.to(device), y.to(device)
            with torch.no_grad():
                y_hat = model(x, m)
                if model.mode == 'RAW':
                    y_hat = y_hat.transpose(1, 2).unsqueeze(-1)
                elif model.mode == 'MOL':
                    y = y.float()
                y = y.unsqueeze(-1)
                loss = self.loss_func(y_hat, y)
                val_loss += loss.item()
        return val_loss / len(val_set)

    @ignore_exception
    def generate_samples(self, model, session) -> Tuple[float, list]:
        """
        Generates audio samples to cherry-pick models. To evaluate audio quality
        we calculate the l1 distance between mels of predictions and targets.
        """
        model.eval()
        mel_losses = []
        gen_wavs = []
        device = next(model.parameters()).device
        for i, (m, x) in enumerate(session.val_set_samples, 1):
            if i > hp.voc_gen_num_samples:
                break
            x = x[0].numpy()
            bits = 16 if hp.voc_mode == 'MOL' else hp.bits
            if hp.mu_law and hp.voc_mode != 'MOL':
                x = decode_mu_law(x, 2 ** bits, from_labels=True)
            else:
                x = label_2_float(x, bits)
            gen_wav = model.generate(
                mels=m, save_path=None, batched=hp.voc_gen_batched,
                target=hp.voc_target, overlap=hp.voc_overlap,
                mu_law=hp.mu_law, silent=True)

            gen_wavs.append(gen_wav)
            y_mel = raw_melspec(x.squeeze())
            y_mel = torch.tensor(y_mel).to(device)
            y_hat_mel = raw_melspec(gen_wav)
            y_hat_mel = torch.tensor(y_hat_mel).to(device)
            loss = F.l1_loss(y_hat_mel, y_mel)
            mel_losses.append(loss.item())

            self.writer.add_audio(
                tag=f'Validation_Samples/target_{i}', snd_tensor=x,
                global_step=model.step, sample_rate=hp.sample_rate)
            self.writer.add_audio(
                tag=f'Validation_Samples/generated_{i}',
                snd_tensor=gen_wav, global_step=model.step, sample_rate=hp.sample_rate)

        for i, (mel_loss, g_wav, m_step) in enumerate(self.top_k_models, 1):
            self.writer.add_audio(
                tag=f'Top_K_Models/generated_top_{i}',
                snd_tensor=g_wav, global_step=m_step, sample_rate=hp.sample_rate)

        return sum(mel_losses) / len(mel_losses), gen_wavs[0]

    def save_top_models(self, mel_loss, gen_wav, model):
        """ Keeps track of top k models saves them according to their current rank """
        print()
        for j, (l, g, m) in enumerate(self.top_k_models):
            print(f'{j} {l} {m}')
        if len(self.top_k_models) < hp.voc_keep_top_k or mel_loss < self.top_k_models[-1][0]:
            self.top_k_models.append((mel_loss, gen_wav, model.get_step()))
            rank_key = self.top_k_models.__getitem__
            new_ranking = sorted(range(len(self.top_k_models)), key=rank_key)
            for old_rank, new_rank in enumerate(new_ranking):
                if new_rank + 1 > hp.voc_keep_top_k:
                    continue
                m_step = self.top_k_models[old_rank][2] // 1000
                old_file = self.paths.voc_checkpoints / f'wave_top_{old_rank + 1}_step{m_step}K_weights.pyt'
                new_file = self.paths.voc_checkpoints / f'wave_top_{new_rank + 1}_step{m_step}K_weights.pyt'
                print(f'{old_rank} {new_rank} {old_file.name} {new_file.name}')
                if os.path.exists(old_file):
                    os.rename(old_file, new_file)
                else:
                    model.save(new_file)
            self.top_k_models.sort(key=lambda t: t[0])
            self.top_k_models = self.top_k_models[:hp.voc_keep_top_k]