"""Shared exceptions for the CLI and domain modules."""



class SyncwheelError(Exception):
    pass


class ManifestDurabilityError(SyncwheelError):
    """The manifest was replaced, but durable directory sync could not be proven."""


class DerivedProvenanceDurabilityError(SyncwheelError):
    """The provenance store was replaced, but directory durability is unknown."""


class CoordinationRemoteRejection(SyncwheelError):
    """The coordination remote refused the atomic push and nothing moved."""


class IntegrationReconciliationLeaseDrift(SyncwheelError):
    """A prepared reconciliation lost an input lease before moving its ref."""


class ControlManifestAlignmentDrift(SyncwheelError):
    """The integration ref moved after the control-manifest decision point."""

    def __init__(self, message, observed_tip):
        super().__init__(message)
        self.observed_tip = observed_tip
