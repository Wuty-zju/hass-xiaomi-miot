"""Optional gateway routes over the existing account scene lifecycle.

Only this layer schedules home qualification. Buttons never query a gateway
list, and a post-dispatch unknown result cannot select a second transport.
"""

import asyncio
import logging

from .const import CONF_CONN_MODE, CONF_SCENE_GATEWAY_MODE
from .scene_gateway import GatewayRejected, GatewayResultUnknown, GatewayUnavailable, scene_gateway_enabled
from .manual_scenes import ManualScenes, scene_error, scene_key

_LOGGER = logging.getLogger(__name__)
RETRY_DELAYS = (3, 15, 60, 300)


class GatewayScenes(ManualScenes):
    def __init__(self, entry):
        super().__init__(entry)
        self._workers = {}
        self._retired = set()
        self._snapshots = {}

    @property
    def gateway_enabled(self):
        return self.enabled and scene_gateway_enabled(self.entry)

    def _snapshot(self, scene):
        backend = getattr(self.entry, 'local_gateway', None)
        current = self._snapshots.get((scene['owner_uid'], scene['home_id']))
        if current and current[0] is backend and current[1].available(scene):
            return current[1]
        return None

    def route(self, key):
        if not self.enabled or key not in self._scenes:
            return None
        if not self.gateway_enabled:
            return super().route(key)
        if self._snapshot(self._scenes[key]):
            return 'local'
        if self.entry.get_config(CONF_CONN_MODE) == 'auto':
            return super().route(key)
        return None

    def notify(self):
        super().notify()
        if self._closed:
            return
        backend = getattr(self.entry, 'local_gateway', None)
        homes = {}
        if self.gateway_enabled and callable(getattr(backend, 'qualify', None)):
            homes = {(s['owner_uid'], s['home_id']): s for s in self._scenes.values()}
        for scope, (owner, wake, task) in list(self._workers.items()):
            if scope not in homes or owner is not backend:
                self._workers.pop(scope)
                self._snapshots.pop(scope, None)
                task.cancel()
                self._retired.add(task)
        for scope, home in homes.items():
            if scope not in self._workers:
                wake = asyncio.Event()
                task = self.hass.async_create_background_task(
                    self._qualify_home(scope, dict(home), backend, wake),
                    'xiaomi_miot gateway scene qualification', eager_start=False,
                )
                self._workers[scope] = backend, wake, task
                task.add_done_callback(self._worker_done)

    def _worker_done(self, task):
        self._retired.discard(task)
        if not task.cancelled() and task.exception():
            _LOGGER.error('Gateway scene qualification task failed', exc_info=task.exception())

    def _changed(self, scope, wake):
        worker = self._workers.get(scope)
        if not self._closed and worker and worker[1] is wake:
            # Current-session checks revoke eligibility synchronously; notify
            # the entity and wake one background reader, not an execution.
            super().notify()
            wake.set()

    async def _qualify_home(self, scope, home, backend, wake):
        failures = 0
        unsubscribe = None
        try:
            while self.gateway_enabled:
                wake.clear()
                try:
                    snapshot = await backend.qualify(home)
                except (GatewayUnavailable, OSError, TimeoutError):
                    self._snapshots.pop(scope, None)
                    failures += 1
                    delay = RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)]
                    if failures == 3:
                        _LOGGER.warning('Gateway scene qualification remains unavailable')
                    else:
                        _LOGGER.debug('Gateway scene qualification temporarily unavailable')
                else:
                    if not self.gateway_enabled:
                        return
                    self._snapshots[scope] = backend, snapshot
                    if unsubscribe:
                        unsubscribe()
                    unsubscribe = snapshot.watch(lambda: self._changed(scope, wake))
                    if failures >= 3:
                        _LOGGER.info('Gateway scene qualification recovered')
                    failures = 0
                    delay = 540  # Refresh before the backend's 600-second expiry.
                super().notify()
                try:
                    await asyncio.wait_for(wake.wait(), delay)
                except TimeoutError:
                    pass
        finally:
            if unsubscribe:
                unsubscribe()

    async def async_execute(self, scene):
        key = scene_key(scene)
        channel = self.route(key)
        if channel == 'local':
            snapshot = self._snapshot(scene)
            try:
                if snapshot is None:
                    raise GatewayUnavailable('gateway qualification changed before dispatch')
                return await snapshot.run(scene)
            except GatewayRejected:
                return False
            except GatewayUnavailable as exc:
                scope = scene['owner_uid'], scene['home_id']
                self._snapshots.pop(scope, None)
                if worker := self._workers.get(scope):
                    worker[1].set()
                super().notify()
                if (self.entry.get_config(CONF_CONN_MODE) != 'auto'
                        or super().route(key) is None):
                    raise scene_error('scene_unavailable') from exc
                # This exception is guaranteed to precede MQTT publication.
            except GatewayResultUnknown as exc:
                raise scene_error('scene_result_unknown') from exc
        elif channel is None:
            raise scene_error('scene_unavailable')
        return await super().async_execute(scene)

    async def async_close(self, event=None):
        if self._closed:
            return
        tasks = self._retired | {item[2] for item in self._workers.values()}
        self._workers.clear()
        self._snapshots.clear()
        for task in tasks:
            task.cancel()
        await super().async_close(event)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._retired.clear()
        if event is not None and (backend := getattr(self.entry, 'local_gateway', None)):
            await backend.close()
