"""Repair flows for the ASIC Miner integration.

Currently a single fixable issue: ``timezone_<ip>``, raised when a miner's
timezone no longer matches Home Assistant's configured zone (e.g. after a DST
change on a VNish miner, or in "repair" mode where corrections are not applied
automatically). Confirming the fix applies the correct timezone (forced auto,
with read-back verification) and clears the issue.

Home Assistant auto-discovers this module via ``async_create_fix_flow``.
"""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .coordinator import MinerCoordinator
from .timezone import async_sync_timezone


class TimezoneRepairFlow(RepairsFlow):
    """Confirm-and-fix flow for a ``timezone_<ip>`` mismatch."""

    def __init__(self, ip: str) -> None:
        self._ip = ip

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            await self._apply_fix()
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=None)

    async def _apply_fix(self) -> None:
        """Locate the coordinator for this miner and force the correct timezone.

        Fully defensive: a missing coordinator (entry unloaded) or any sync error
        must not break the repairs UI. ``async_sync_timezone`` clears the issue on
        a confirmed correction; if no matching coordinator is found, drop the
        issue anyway so a stale one cannot linger.
        """
        coordinator = self._find_coordinator()
        if coordinator is None:
            from homeassistant.helpers import issue_registry as ir

            ir.async_delete_issue(self.hass, DOMAIN, f"timezone_{self._ip}")
            return
        await async_sync_timezone(self.hass, coordinator, force_auto=True)

    def _find_coordinator(self) -> MinerCoordinator | None:
        for coordinator in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, MinerCoordinator) and coordinator.ip == self._ip:
                return coordinator
        return None


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    """Return the repair flow for a given issue id."""
    if issue_id.startswith("timezone_"):
        return TimezoneRepairFlow(issue_id[len("timezone_"):])
    # Unknown/foreign issue id: fall back to a plain confirm flow.
    return ConfirmRepairFlow()
