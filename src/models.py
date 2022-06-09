import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
import src.utils as utils

class ExplicitPCN(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim
        # initialize parameters
        self.S = nn.Parameter(torch.eye(dim))

    def forward(self, X):
        return X

    def learning(self, X):
        errs = torch.matmul(self.forward(X), torch.linalg.inv(self.S).T)
        grad_S = 0.5 * (torch.matmul(errs.T, errs) / X.shape[0] - torch.linalg.inv(self.S))

        self.S.grad = -grad_S

    def inference(self, X_c):
        errs_X = torch.matmul(self.forward(X_c), torch.linalg.inv(self.S).T)
        delta_X = -errs_X

        return delta_X

class RecPCN(nn.Module):
    def __init__(self, dim, dendrite=True, mode='linear'):
        super().__init__()
        self.dim = dim
        self.dendrite = dendrite
        # initialize parameters
        self.Wr = nn.Parameter(torch.zeros((dim, dim)))
        self.mu = nn.Parameter(torch.zeros((dim,)))

        if mode == 'linear':
            self.nonlin = utils.Linear()
        elif mode == 'rate':
            self.nonlin = utils.Sigmoid()
        elif mode == 'binary':
            self.nonlin = utils.Binary()
        else:
            raise ValueError("no such nonlinearity!")

    def forward(self, X):
        preds = torch.matmul(self.nonlin(X), self.Wr.t()) + self.mu
        return preds
        
    def learning(self, X):
        preds = self.forward(X)
        errs = X - preds
        grad_Wr = torch.matmul(errs.T, self.nonlin(X)).fill_diagonal_(0)
        grad_mu = torch.sum(errs, dim=0)

        self.Wr.grad = -grad_Wr
        self.mu.grad = -grad_mu
        self.train_mse = torch.mean(errs**2)

    def inference(self, X_c):
        errs_X = X_c - self.forward(X_c)
        delta_X = -errs_X if self.dendrite else (-errs_X + torch.matmul(errs_X, self.Wr))

        return delta_X
        

class MultilayerPCN(nn.Module):
    def __init__(self, nodes, nonlin, Dt, lamb=0, use_bias=False):
        super().__init__()
        self.n_layers = len(nodes)
        self.layers = nn.Sequential()
        for l in range(self.n_layers-1):
            self.layers.add_module(f'layer_{l}', nn.Linear(
                in_features=nodes[l],
                out_features=nodes[l+1],
                bias=use_bias,
            ))

        self.mem_dim = nodes[0]
        self.memory = nn.Parameter(torch.zeros((nodes[0],)))
        self.Dt = Dt

        if nonlin == 'Tanh':
            nonlin = utils.Tanh()
        elif nonlin == 'ReLU':
            nonlin = utils.ReLU()
        self.nonlins = [nonlin] * (self.n_layers - 1)
        self.use_bias = use_bias
        self.lamb = lamb

    def initialize(self):
        self.val_nodes = [[] for _ in range(self.n_layers)]
        self.preds = [[] for _ in range(self.n_layers)]
        self.errs = [[] for _ in range(self.n_layers)]

    def forward(self):
        val = self.memory.clone().detach()
        for l in range(self.n_layers-1):
            val = self.layers[l](self.nonlins[l](val))
        return val

    def update_err_nodes(self):
        raise NotImplementedError()

    def set_nodes(self, batch_inp):
        # computing val nodes
        self.val_nodes[0] = self.memory.clone().detach()
        for l in range(1, self.n_layers-1):
            self.val_nodes[l] = self.layers[l-1](self.nonlins[l-1](self.val_nodes[l-1]))
        self.val_nodes[-1] = batch_inp.clone()

        # computing error nodes
        self.update_err_nodes()

    def update_val_nodes(self, update_mask, recon=False):
        with torch.no_grad():
            for l in range(0, self.n_layers-1):
                derivative = self.nonlins[l].deriv(self.val_nodes[l])
                penalty = self.lamb if l == 0 else 0.
                delta = -self.errs[l] - penalty * torch.sign(self.val_nodes[l]) + derivative * torch.matmul(self.errs[l+1], self.layers[l].weight)
                self.val_nodes[l] = self.val_nodes[l] + self.Dt * delta
            if recon:
                # relax sensory layer value nodes if its corrupted (during reconstruction phase)
                self.val_nodes[-1] = self.val_nodes[-1] + self.Dt * (-self.errs[-1] * update_mask)

            self.update_err_nodes()

    def update_grads(self):
        raise NotImplementedError()

    def train_pc_generative(self, batch_inp, n_iters, update_mask):
        self.initialize()
        self.set_nodes(batch_inp)
        for itr in range(n_iters):
            self.update_val_nodes(update_mask)
        self.update_grads()

    def test_pc_generative(self, corrupt_inp, n_iters, update_mask, sensory=True):
        self.initialize()
        self.set_nodes(corrupt_inp)
        for itr in range(n_iters):
            self.update_val_nodes(update_mask, recon=True)

        if sensory:
            return self.val_nodes[-1]
        else:
            return self.preds[-1]
    
    
class HierarchicalPCN(MultilayerPCN):
    def __init__(self, nodes, nonlin, Dt, init_std=0., lamb=0, use_bias=False):
        super().__init__(nodes, nonlin, Dt, lamb, use_bias)
        self.memory = nn.Parameter(init_std * torch.randn((self.mem_dim,)))

    def update_err_nodes(self):
        for l in range(0, self.n_layers):
            if l == 0:
                self.preds[l] = self.memory.clone().detach()
            else:
                self.preds[l] = self.layers[l-1](self.nonlins[l-1](self.val_nodes[l-1]))
            self.errs[l] = self.val_nodes[l] - self.preds[l]

    def update_grads(self):
        self.memory.grad = -torch.sum(self.errs[0], axis=0)
        for l in range(self.n_layers-1):
            grad_w = -torch.matmul(self.errs[l+1].t(), self.nonlins[l](self.val_nodes[l]))
            self.layers[l].weight.grad = grad_w
            if self.use_bias:
                self.layers[l].bias.grad = -torch.sum(self.errs[l+1], axis=0)


class HybridPCN(MultilayerPCN):
    def __init__(self, nodes, nonlin, Dt, init_std=0., init_std_Wr=0., lamb=0, use_bias=False):
        super().__init__(nodes, nonlin, Dt, lamb, use_bias)
        self.memory = nn.Parameter(init_std * torch.randn((self.mem_dim,)))
        self.Wr = nn.Parameter(init_std_Wr * torch.randn((self.mem_dim, self.mem_dim)))

    def update_err_nodes(self):
        for l in range(0, self.n_layers):
            if l == 0:
                self.preds[l] = self.memory.clone().detach() + torch.matmul(self.val_nodes[l], self.Wr.t())
            else:
                self.preds[l] = self.layers[l-1](self.nonlins[l-1](self.val_nodes[l-1]))
            self.errs[l] = self.val_nodes[l] - self.preds[l]

    def update_grads(self):
        self.memory.grad = -torch.sum(self.errs[0], axis=0)
        self.Wr.grad = -torch.matmul(self.errs[0].t(), self.val_nodes[0]).fill_diagonal_(0)

        for l in range(self.n_layers-1):
            grad_w = -torch.matmul(self.errs[l+1].t(), self.nonlins[l](self.val_nodes[l]))
            self.layers[l].weight.grad = grad_w
            if self.use_bias:
                self.layers[l].bias.grad = -torch.sum(self.errs[l+1], axis=0)


class AutoEncoder(nn.Module):
    def __init__(self, dim, latent_dim):
        super().__init__()
        self.dim = dim
        self.latent_dim = latent_dim
        # initialize parameters
        self.We = nn.Linear(dim, latent_dim, bias=False)
        self.Wd = nn.Linear(latent_dim, dim, bias=False)

        self.tanh = nn.Tanh()
        
    def forward(self, X):
        return self.tanh(self.Wd(self.tanh(self.We(X))))
    