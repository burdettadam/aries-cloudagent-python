"""Manage Indy-SDK profile interaction."""

import logging

from typing import Any, Mapping
from weakref import ref

from ...cache.base import BaseCache
from ...config.injection_context import InjectionContext
from ...config.provider import ClassProvider
from ...core.profile import Profile, ProfileManager, ProfileSession
from ...ledger.base import BaseLedger
from ...ledger.indy import IndySdkLedger, IndySdkLedgerPool
from ...storage.base import BaseStorage
from ...wallet.base import BaseWallet
from ...wallet.indy import IndySdkWallet

from ..holder import IndyHolder
from ..issuer import IndyIssuer
from ..verifier import IndyVerifier

from .wallet_setup import IndyWalletConfig, IndyOpenWallet

LOGGER = logging.getLogger(__name__)


class IndySdkProfile(Profile):
    """Provide access to Indy profile interaction methods."""

    BACKEND_NAME = "indy"

    def __init__(self, opened: IndyOpenWallet, context: InjectionContext = None):
        """Create a new IndyProfile instance."""
        super().__init__(context=context, name=opened.name, created=opened.created)
        self.opened = opened
        self.ledger_pool: IndySdkLedgerPool = None
        self.init_ledger_pool()
        self.bind_providers()

    @property
    def name(self) -> str:
        """Accessor for the profile name."""
        return self.opened.name

    @property
    def wallet(self) -> IndyOpenWallet:
        """Accessor for the opened wallet instance."""
        return self.opened

    def init_ledger_pool(self):
        """Initialize the ledger pool."""
        if self.settings.get("ledger.disabled"):
            LOGGER.info("Ledger support is disabled")
            return

        pool_name = self.settings.get("ledger.pool_name", "default")
        keepalive = int(self.settings.get("ledger.keepalive", 5))
        read_only = bool(self.settings.get("ledger.read_only", False))
        if read_only:
            LOGGER.error("Note: setting ledger to read-only mode")
        genesis_transactions = self.settings.get("ledger.genesis_transactions")
        cache = self.context.injector.inject(BaseCache, required=False)
        self.ledger_pool = IndySdkLedgerPool(
            pool_name,
            keepalive=keepalive,
            cache=cache,
            genesis_transactions=genesis_transactions,
            read_only=read_only,
        )

    def bind_providers(self):
        """Initialize the profile-level instance providers."""
        injector = self._context.injector
        injector.bind_provider(
            IndyHolder,
            ClassProvider(
                "aries_cloudagent.indy.sdk.holder.IndySdkHolder", self.opened
            ),
        )
        injector.bind_provider(
            IndyIssuer,
            ClassProvider("aries_cloudagent.indy.sdk.issuer.IndySdkIssuer", ref(self)),
        )

        if self.ledger_pool:
            ledger = IndySdkLedger(self.ledger_pool, IndySdkWallet(self.opened))

            injector.bind_instance(BaseLedger, ledger)
            injector.bind_provider(
                IndyVerifier,
                ClassProvider(
                    "aries_cloudagent.indy.sdk.verifier.IndySdkVerifier",
                    ledger,
                ),
            )

    def session(self, context: InjectionContext = None) -> "ProfileSession":
        """Start a new interactive session with no transaction support requested."""
        return IndySdkProfileSession(self, context=context)

    def transaction(self, context: InjectionContext = None) -> "ProfileSession":
        """
        Start a new interactive session with commit and rollback support.

        If the current backend does not support transactions, then commit
        and rollback operations of the session will not have any effect.
        """
        return IndySdkProfileSession(self, context=context)

    async def close(self):
        """Close the profile instance."""
        if self.opened:
            await self.opened.close()
            self.opened = None


class IndySdkProfileSession(ProfileSession):
    """An active connection to the profile management backend."""

    def __init__(
        self,
        profile: Profile,
        *,
        context: InjectionContext = None,
        settings: Mapping[str, Any] = None
    ):
        """Create a new IndySdkProfileSession instance."""
        super().__init__(profile=profile, context=context, settings=settings)

    async def _setup(self):
        """Create the session or transaction connection, if needed."""
        await super()._setup()
        injector = self._context.injector
        injector.bind_provider(
            BaseWallet, ClassProvider(IndySdkWallet, self.profile.opened)
        )
        injector.bind_provider(
            BaseStorage,
            ClassProvider(
                "aries_cloudagent.storage.indy.IndySdkStorage", self.profile.opened
            ),
        )


class IndySdkProfileManager(ProfileManager):
    """Manager for Indy-SDK wallets."""

    async def provision(self, config: Mapping[str, Any] = None) -> Profile:
        """Provision a new instance of a profile."""
        indy_config = IndyWalletConfig(config)
        opened = await indy_config.create_wallet()
        return IndySdkProfile(opened, self.context)

    async def open(self, config: Mapping[str, Any] = None) -> Profile:
        """Open an instance of an existing profile."""
        indy_config = IndyWalletConfig(config)
        opened = await indy_config.open_wallet()
        return IndySdkProfile(opened, self.context)
