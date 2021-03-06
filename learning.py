import os 
import click
import cv2
from loguru import logger

import torch as th 
import torch.nn as nn 
import torch.optim as optim 

from torch.utils.data import DataLoader
from torchvision.models import vgg16

import torch.distributed as td 
import torch.multiprocessing as tm 

from torch.utils.data import DataLoader as DTL 
from torch.nn.parallel import DistributedDataParallel as DDP 
from torch.utils.data.distributed import DistributedSampler as DSP  

from dataset import Source 
from modelization.discriminator import Discriminator
from modelization.generator import Generator
from libraries.strategies import * 

from os import path 


def train_0(device, source_path, nb_epochs, bt_size, path_to_dump):
    G = Generator(nb_blocks=8, nb_channels=64, scale_factor=4).to(device)
    D = Discriminator(in_channels=3, nb_channels=64, nb_blocks=8, nb_neurons_on_dense=1024).to(device)
    
    optim_G = optim.Adam(params=G.parameters(), lr=0.0004, betas=(0.9, 0.999))
    optim_D = optim.Adam(params=D.parameters(), lr=0.0004, betas=(0.9, 0.999))
    mse_criterion = nn.MSELoss().to(device)
    adv_criterion = nn.BCELoss().to(device)
    
    vgg = vgg16(pretrained=True)
    vgg16_FE = nn.Sequential(*list(vgg.features)).eval().to(device)

    source = Source(source_path, (256, 256))
    loader = DTL(dataset=source, shuffle=True, batch_size=bt_size)
    
    msg_fmt = '[%03d/%03d]:%05d | ED => %07.3f | EG => %07.3f'
    epoch_counter = 0 
    while epoch_counter < nb_epochs:
        for iteration, (I_LR, I_HR) in enumerate(loader):
            # move training data to cuda
            I_LR = I_LR.to(device)
            I_HR = I_HR.to(device)

            # create real and fake labels 
            RL = th.ones(I_LR.shape[0]).float().to(device)
            FL = th.zeros(I_LR.shape[0]).float().to(device)

            # train generator 
            optim_G.zero_grad()
            I_SR = G(I_LR)
            I_SR_FE = vgg16_FE(I_SR)
            I_HR_FE = vgg16_FE(I_HR)
            L_pix = mse_criterion(I_SR, I_HR)
            L_vgg = mse_criterion(I_SR_FE, I_HR_FE.detach())
            L_adv = adv_criterion(D(I_SR), RL)
            L_gen = L_pix + 0.006 * L_vgg + 0.001 * L_adv
            L_gen.backward()
            optim_G.step()

            # train discriminator
            optim_D.zero_grad()
            E_D_IHR = adv_criterion(D(I_HR), RL)
            E_D_ISR = adv_criterion(D(I_SR.detach()), FL)
            E_dis = (E_D_IHR + E_D_ISR) * 0.5
            E_dis.backward()
            optim_D.step()

            logger.debug(msg_fmt % (epoch_counter, nb_epochs, iteration, E_dis.item(), L_gen.item()))
            if iteration % 200 == 0:
                logger.debug('An image was saved...!')
                I_HR = I_HR.cpu()
                I_LR = I_LR.cpu()
                I_SR = I_SR.cpu()
                I_LR = to_grid(nn.functional.interpolate(I_LR, scale_factor=4), nb_rows=1)
                I_SR = to_grid(I_SR, nb_rows=1)
                I_HR = to_grid(I_HR, nb_rows=1)
                I_LS = th2cv(th.cat((I_LR, I_SR, I_HR), -1)) * 255
                cv2.imwrite(f'{path_to_dump}/img_{epoch_counter:02d}_{iteration:03d}.jpg', I_LS)
        epoch_counter += 1

        if epoch_counter % 10 == 0:
            th.save(G, f'{epoch_counter:02d}_generator.pt')
    
    logger.debug(' ... end training ... ')
    th.save(G, 'generator.pt')
    th.save(D, 'discriminator.pt')


def train_1(gpu_idx, node_idx, world_size, source_path, nb_epochs, bt_size, server_config, path_to_dump):
    worker_rank = node_idx + gpu_idx
    td.init_process_group(
        backend='nccl',
        init_method=server_config, 
        world_size=world_size,
        rank=worker_rank
    )

    th.manual_seed(0)
    th.cuda.set_device(gpu_idx)

    G = Generator(nb_blocks=8, nb_channels=64, scale_factor=4).cuda(gpu_idx)
    G = DDP(module=G, device_ids=[gpu_idx], broadcast_buffers=False)

    D = Discriminator(in_channels=3, nb_channels=64, nb_blocks=8, nb_neurons_on_dense=1024).cuda(gpu_idx)
    D = DDP(module=D, device_ids=[gpu_idx], broadcast_buffers=False)

    optim_G = optim.Adam(params=G.parameters(), lr=0.0002, betas=(0.5, 0.999))
    optim_D = optim.Adam(params=D.parameters(), lr=0.0002, betas=(0.5, 0.999))
    mse_criterion = nn.MSELoss().cuda(gpu_idx)
    adv_criterion = nn.BCELoss().cuda(gpu_idx)
    
    vgg = vgg16(pretrained=True)
    vgg16_FE = nn.Sequential(*list(vgg.features)).eval().cuda(gpu_idx)
    vgg16_FE = DDP(vgg16_FE, device_ids=[gpu_idx], broadcast_buffers=False)

    source = Source(source_path, (256, 256))
    picker = DSP(dataset=source, num_replicas=world_size, rank=worker_rank) 
    loader = DTL(dataset=source, shuffle=False, batch_size=bt_size, sampler=picker)
    
    msg_fmt = '(%03d) [%03d/%03d]:%05d | ED => %07.3f | EG => %07.3f'
    epoch_counter = 0 
    while epoch_counter < nb_epochs:
        for iteration, (I_LR, I_HR) in enumerate(loader):
            # move training data to cuda
            I_LR = I_LR.cuda(gpu_idx)
            I_HR = I_HR.cuda(gpu_idx)

            # create real and fake labels 
            RL = th.ones(I_LR.shape[0]).float().cuda(gpu_idx)
            FL = th.zeros(I_LR.shape[0]).float().cuda(gpu_idx)

            # train generator 
            optim_G.zero_grad()
            I_SR = G(I_LR)
            I_SR_FE = vgg16_FE(I_SR)
            I_HR_FE = vgg16_FE(I_HR)
            L_pix = mse_criterion(I_SR, I_HR)
            L_vgg = mse_criterion(I_SR_FE, I_HR_FE)
            L_adv = adv_criterion(D(I_SR), RL)
            L_gen = L_vgg + 0.005 * L_pix + 0.01 * L_adv
            L_gen.backward()
            optim_G.step()

            # train discriminator
            optim_D.zero_grad()
            E_D_IHR = adv_criterion(D(I_HR), RL)
            E_D_ISR = adv_criterion(D(I_SR.detach()), FL)
            E_dis = (E_D_IHR + E_D_ISR) * 0.5
            E_dis.backward()
            optim_D.step()

            logger.debug(msg_fmt % (gpu_idx, epoch_counter, nb_epochs, iteration, E_dis.item(), L_gen.item()))
            if iteration % 200 == 0 and gpu_idx == 0:
                logger.debug('An image was saved...!')
                I_HR = I_HR.cpu()
                I_LR = I_LR.cpu()
                I_SR = I_SR.cpu()
                I_LR = to_grid(nn.functional.interpolate(I_LR, scale_factor=4), nb_rows=1)
                I_SR = to_grid(I_SR, nb_rows=1)
                I_HR = to_grid(I_HR, nb_rows=1)
                I_LS = th2cv(th.cat((I_LR, I_SR, I_HR), -1)) * 255
                cv2.imwrite(f'{path_to_dump}/img_{epoch_counter:02d}_{iteration:03d}.jpg', I_LS)
        epoch_counter += 1

    
    if gpu_idx == 0:
        logger.debug(' ... end training ... ')
        th.save(G, 'generator.pt')
        th.save(D, 'discriminator.pt')

@click.command()
@click.option('--nb_nodes', help='number of nodes', type=int)
@click.option('--nb_gpus', help='number of gpus core per nodes', type=int)
@click.option('--current_rank', help='rank of current node', type=int)
@click.option('--source_path', help='path to source data', type=str)
@click.option('--nb_epochs', help='number of epochs during training', type=int)
@click.option('--bt_size', help='size of batched data', type=int)
@click.option('--server_config', help='tcp://address:port', type=str)
@click.option('--path_to_dump', help='sample images will be stored on dump', type=click.Path(False))
@click.pass_context
def multiple_gpu(ctx, nb_nodes, nb_gpus, current_rank, source_path, nb_epochs, bt_size, server_config, path_to_dump):
    if not path.isdir(path_to_dump):
        os.mkdir(path_to_dump)

    if th.cuda.is_available():
        logger.debug('The training mode will be on GPU')
        logger.debug(f'{th.cuda.device_count()} were detected ...!')
        tm.spawn(
            train, 
            nprocs=nb_gpus,
            args=(current_rank * nb_gpus, nb_nodes * nb_gpus, source_path, nb_epochs, bt_size, server_config, path_to_dump)
        )
    else:
        logger.debug('No GPU was detected ...! try to use --single_gpu(with cpu support)')


@click.command()
@click.option('--gpu_idx')
@click.option('--source_path', help='path to source data', type=str)
@click.option('--nb_epochs', help='number of epochs during training', type=int)
@click.option('--bt_size', help='size of batched data', type=int)
@click.option('--path_to_dump', help='sample images will be stored on dump', type=click.Path(False))
@click.pass_context
def single_gpu(ctx, gpu_idx, source_path, nb_epochs, bt_size, path_to_dump):
    if not path.isdir(path_to_dump):
        os.mkdir(path_to_dump)
    if th.cuda.is_available and th.cuda.device_count() > 0:
        device = th.device(f"cuda:{gpu_idx}") 
    else:
        device = 'cpu'
    train_0(device, source_path, nb_epochs, bt_size, path_to_dump)

@click.group(chain=False, invoke_without_command=True)
@click.pass_context
def main_command(ctx):
    if not ctx.invoked_subcommand:
        logger.debug('main command ...!')
    
main_command.add_command(single_gpu)
main_command.add_command(multiple_gpu)

if __name__ == '__main__':
    main_command(obj={})