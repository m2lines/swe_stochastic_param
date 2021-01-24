#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Dec  5 15:44:07 2020

@author: arthur
"""

"""
Low-resolution, run with NN parameterization
"""
import sys
sys.path.append('/home/ag7531/code')
sys.path.append('/home/ag7531/code/subgrid')
import torch
import logging

import numpy as np
import matplotlib.pyplot as plt
import matplotlib
import mlflow
import argparse

from shallowwater import ShallowWaterModel
from shallowwaterParameterized import (WaterModelWithDLParameterization,
                                       Parameterization)
from subgrid.models.utils import load_model_cls
from subgrid.analysis.utils import select_run, select_experiment
from subgrid.testing.utils import pickle_artifact
from netCDF4 import Dataset
from os.path import join
import tempfile

from utils import BoundaryCondition

# Make temporary dir to save outputs
temp_dir = tempfile.mkdtemp(dir='/scratch/ag7531/temp/')

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

parser = argparse.ArgumentParser()
parser.add_argument('nyears', type=int,
                    help='Number of years the model is spun up for')
parser.add_argument('factor', type=int,
                    help='Coarse-graining factor')
parser.add_argument('--every', type=int, default=1,
                    help='Parameter passed to the stochastic parameterization')
parser.add_argument('--every_noise', type=int, default=1,
                    help='Parameter passed to the stochastic parameterization')
parser.add_argument('--param_amp', type=int, default=1.,
                    help='Multiplication factor applied to parameterization')
parser.add_argument('--boundary', type=str, default='no-slip',
                    help='either \'no-slip\' or \'free-slip\'')
parser.add_argument('--force-zero-sum', type=bool, default=False,
                    help='Whether we enforce that the integrate forcing is '
                         'zero')
script_args = parser.parse_args()

n_years = script_args.nyears
factor = script_args.factor
every = script_args.every
every_noise = script_args.every_noise
param_amp = script_args.param_amp
boundary_condition = BoundaryCondition.get(script_args.boundary)
force_zero_sum = script_args.force_zero_sum

from_spinup=False
domain_size = 3840
parameterization = factor > 0

if parameterization:
    mlflow.set_experiment('parameterized')
else:
    mlflow.set_experiment('raw')

mlflow.log_params(dict(n_years=n_years, factor=factor,
                       boundary=boundary_condition.name))
if parameterization:
    mlflow.log_params(dict(param_amp=param_amp, every=every,
                           every_noise=every_noise, zero_sum=force_zero_sum))

model = ShallowWaterModel(output_path=temp_dir,
                          Nx=domain_size // 10 // factor,
                          Ny=domain_size // 10 // factor,
                          Lx=domain_size * 1e3,
                          Ly=domain_size * 1e3,
                          Nt=n_years*360*24*60*60,
                          dump_freq=1*24*60*60, dump_output=True, tau0=0.12,
                          model_name='eddy_permitting',
                          bc=boundary_condition.value)

if parameterization:
    # TODO put into separate function, separate utils file
    # Load the parameterization
    # models_experiment_name = select_experiment()
    # models_experiment = mlflow.get_experiment_by_name(models_experiment_name)
    # models_experiment_id = models_experiment.experiment_id
    cols = ['metrics.test loss', 'start_time', 'params.time_indices',
            'params.model_cls_name', 'params.source.run_id', 'params.submodel']
    # model_run = select_run(sort_by='start_time', cols=cols,
    #                        experiment_ids=[models_experiment_id, ])
    # TODO this is only  a temp fix
    model_run = mlflow.search_runs(experiment_ids=('21',)).loc[11, :]
    model_module_name = model_run['params.model_module_name']
    model_cls_name = model_run['params.model_cls_name']
    logging.info('Creating the neural network model')
    model_cls = load_model_cls(model_module_name, model_cls_name)

    # Load the model's file
    client = mlflow.tracking.MlflowClient()
    model_file = client.download_artifacts(model_run.run_id,
                                           'models/trained_model.pth')
    transformation = pickle_artifact(model_run.run_id, 'models/transformation')
    net = model_cls(2, 4, padding='same')
    net.final_transformation = transformation

    # Load parameters of pre-trained model
    logging.info('Loading the neural net parameters')
    net.cpu()
    net.load_state_dict(torch.load(model_file))
    print('*******************')
    print(net)
    print('*******************')

u, v, eta = model.set_initial_cond( init='rest' )
if parameterization:
    parameterization = Parameterization(net, device, param_amp, every,
                                        force_zero_sum)
    model = WaterModelWithDLParameterization(model, parameterization)

if from_spinup:
    # load high-rez simulation, coarse-grain
    files_dir = '/scratch/ag7531/shallowWaterModel/spinup1'
    u_dataset = Dataset(join(files_dir, 'u_eddy_permitting__10yr_spinup.nc'))
    v_dataset = Dataset(join(files_dir, 'v_eddy_permitting__10yr_spinup.nc'))
    eta_dataset = Dataset(join(files_dir, 'eta_eddy_permitting__10yr_spinup.nc'))
    
    u = u_dataset.variables['u'][-1, ...]
    v = v_dataset.variables['v'][-1, ...]
    eta = eta_dataset.variables['eta'][-1, ...]
    
    u = coarsen(u, 4)
    v = coarsen(v, 4)
    eta = coarsen(eta, 4)
    
    u = np.squeeze(u.reshape((-1, 1)))
    v = np.squeeze(v.reshape((-1, 1)))
    eta = np.squeeze(eta.reshape((-1, 1)))
    model.u = u
    model.v = v
    model.eta = eta

last_percent = None
for i in range( model.N_iter ) :
    percent = int(1000.0 * float(i) / model.N_iter)
    if percent != last_percent:
        print( "{}%".format( percent / 10 ) )
        last_percent = percent
        # plt.imshow(model.u2mat(u), vmin=-0.5, vmax=0.5, cmap='PuOr')
        # plt.show(block=False)
        # plt.draw()

    u_new, v_new, eta_new = model.integrate_forward( u, v, eta )
    
    if u_new is None :
        print( "Integration finished!" )
        break
    
    u = u_new
    v = v_new
    eta = eta_new
mlflow.log_artifacts(temp_dir)
print('Done.')