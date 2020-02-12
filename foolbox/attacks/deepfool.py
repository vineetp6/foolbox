from typing import Union, Optional, Tuple
import eagerpy as ep
import logging
from abc import ABC
from abc import abstractmethod

from ..devutils import flatten
from ..devutils import atleast_kd

from ..models import Model

from ..criteria import Criterion

from .base import MinimizationAttack
from .base import T
from .base import get_criterion


class DeepFoolAttack(MinimizationAttack, ABC):
    """A simple and fast gradient-based adversarial attack.

    Implementes DeepFool introduced in [1]_.

    Args:
        p: Lp-norm that should be minimzed, must be 2 or np.inf.
        candidates: Limit on the number of the most likely classes that should
            be considered. A small value is usually sufficient and much faster.
        overshoot
        steps: Maximum number of steps to perform.

    References
    ----------
    .. [1] Seyed-Mohsen Moosavi-Dezfooli, Alhussein Fawzi, Pascal Frossard,
           "DeepFool: a simple and accurate method to fool deep neural
           networks", https://arxiv.org/abs/1511.04599

    """

    def __init__(
        self,
        steps: int = 50,
        candidates: Optional[int] = 10,
        overshoot: float = 0.02,
        loss: str = "logits",
    ):
        self.steps = steps
        self.candidates = candidates
        self.overshoot = overshoot
        self.loss = loss

    def __call__(self, model: Model, inputs: T, criterion: Union[Criterion, T],) -> T:
        x, restore_type = ep.astensor_(inputs)
        del inputs

        criterion = get_criterion(criterion)

        min_, max_ = model.bounds

        logits = model(x)
        classes = logits.argsort(axis=-1).flip(axis=-1)
        if self.candidates is None:
            candidates = logits.shape[-1]
        else:
            candidates = min(self.candidates, logits.shape[-1])
            if not candidates >= 2:
                raise ValueError(
                    f"expected the model output to have atleast 2 classes, got {logits.shape[-1]}"
                )
            logging.info(f"Only testing the top-{candidates} classes")
            classes = classes[:, :candidates]

        N = len(x)
        rows = range(N)
        i0 = classes[:, 0]

        if self.loss == "logits":

            def loss_fun(
                x: ep.Tensor, k: int
            ) -> Tuple[ep.Tensor, Tuple[ep.Tensor, ep.Tensor]]:
                logits = model(x)
                ik = classes[:, k]
                l0 = logits[rows, i0]
                lk = logits[rows, ik]
                loss = lk - l0
                return loss.sum(), (loss, logits)

        elif self.loss == "crossentropy":

            def loss_fun(
                x: ep.Tensor, k: int
            ) -> Tuple[ep.Tensor, Tuple[ep.Tensor, ep.Tensor]]:
                logits = model(x)
                ik = classes[:, k]
                l0 = -ep.crossentropy(logits, i0)
                lk = -ep.crossentropy(logits, ik)
                loss = lk - l0
                return loss.sum(), (loss, logits)

        else:
            raise ValueError(
                f"expected loss to be 'logits' or 'crossentropy', got '{self.loss}'"
            )

        loss_aux_and_grad = ep.value_and_grad_fn(x, loss_fun, has_aux=True)

        x0 = x
        p_total = ep.zeros_like(x)
        for _ in range(self.steps):
            # let's first get the logits using k = 1 to see if we are done
            diffs = [loss_aux_and_grad(x, 1)]
            _, (_, logits), _ = diffs[0]

            is_adv = criterion(x, logits)
            if is_adv.all():
                break

            # then run all the other k's as well
            # we could avoid repeated forward passes and only repeat
            # the backward pass, but this cannot currently be done in eagerpy
            diffs += [loss_aux_and_grad(x, k) for k in range(2, candidates)]

            # we don't need the logits
            diffs_ = [(losses, grad) for _, (losses, _), grad in diffs]
            losses = ep.stack([l for l, _ in diffs_], axis=1)
            grads = ep.stack([g for _, g in diffs_], axis=1)
            assert losses.shape == (N, candidates - 1)
            assert grads.shape == (N, candidates - 1) + x0.shape[1:]

            # calculate the distances
            distances = self.get_distances(losses, grads)
            assert distances.shape == (N, candidates - 1)

            # determine the best directions
            best = distances.argmin(axis=1)
            distances = distances[rows, best]
            losses = losses[rows, best]
            grads = grads[rows, best]
            assert distances.shape == (N,)
            assert losses.shape == (N,)
            assert grads.shape == x0.shape

            # apply perturbation
            distances = distances + 1e-4  # for numerical stability
            p_step = self.get_perturbations(distances, grads)
            assert p_step.shape == x0.shape

            p_total += p_step
            # don't do anything for those that are already adversarial
            x = ep.where(
                atleast_kd(is_adv, x.ndim), x, x0 + (1.0 + self.overshoot) * p_total
            )
            x = ep.clip(x, min_, max_)

        return restore_type(x)

    @abstractmethod
    def get_distances(self, losses: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        ...

    @abstractmethod
    def get_perturbations(self, distances: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        ...


class L2DeepFoolAttack(DeepFoolAttack):
    def get_distances(self, losses: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        return abs(losses) / (flatten(grads, keep=2).norms.l2(axis=-1) + 1e-8)

    def get_perturbations(self, distances: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        return (
            atleast_kd(
                distances / (flatten(grads).norms.l2(axis=-1) + 1e-8), grads.ndim,
            )
            * grads
        )


class LinfDeepFoolAttack(DeepFoolAttack):
    def get_distances(self, losses: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        return abs(losses) / (flatten(grads, keep=2).abs().sum(axis=-1) + 1e-8)

    def get_perturbations(self, distances: ep.Tensor, grads: ep.Tensor) -> ep.Tensor:
        return atleast_kd(distances, grads.ndim) * grads.sign()