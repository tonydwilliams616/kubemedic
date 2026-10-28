"""Checks the lab refuses to break anything except a local kind cluster."""
import pytest

from lab.kube import KubeError, ensure_safe_context


def test_kind_context_allowed():
    ensure_safe_context("kind-kubemedic", allow_non_kind=False)


@pytest.mark.parametrize("ctx", ["arn:aws:eks:eu-west-2:123456789012:cluster/prod", "docker-desktop", "prod"])
def test_non_kind_context_refused_by_default(ctx):
    with pytest.raises(KubeError):
        ensure_safe_context(ctx, allow_non_kind=False)


def test_explicit_opt_in_allows_non_kind():
    ensure_safe_context("eks-sandbox", allow_non_kind=True)
