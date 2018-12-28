import asyncio

from typing import List
from collections import defaultdict

from websockets.exceptions import ConnectionClosed
from logbook import Logger

from discord.gateway.state import GatewayState
from discord.gateway.opcodes import OP


log = Logger(__name__)


class ManagerClose(Exception):
    pass


class StateDictWrapper:
    """Wrap a mapping so that any kind of access to the mapping while the
    state manager is closed raises a ManagerClose error"""
    def __init__(self, state_manager, mapping):
        self.state_manager = state_manager
        self._map = mapping

    def _check_closed(self):
        if self.state_manager.closed:
            raise ManagerClose()

    def __getitem__(self, key):
        self._check_closed()
        return self._map[key]

    def __delitem__(self, key):
        self._check_closed()
        del self._map[key]

    def __setitem__(self, key, value):
        if not self.state_manager.accept_new:
            raise ManagerClose()

        self._check_closed()
        self._map[key] = value

    def __iter__(self):
        return self._map.__iter__()

    def pop(self, key):
        return self._map.pop(key)

    def values(self):
        return self._map.values()


class StateManager:
    """Manager for gateway state information."""

    def __init__(self):
        #: closed flag
        self.closed = False

        #: accept new states?
        self.accept_new = True

        # {
        #  user_id: {
        #   session_id: GatewayState,
        #   session_id_2: GatewayState, ...
        #  },
        #  user_id_2: {}, ...
        # }
        self.states = StateDictWrapper(self, defaultdict(dict))

        #: raw mapping from session ids to GatewayState
        self.states_raw = StateDictWrapper(self, {})

    def insert(self, state: GatewayState):
        """Insert a new state object."""
        user_states = self.states[state.user_id]

        log.debug('inserting state: {!r}', state)
        user_states[state.session_id] = state
        self.states_raw[state.session_id] = state

    def fetch(self, user_id: int, session_id: str) -> GatewayState:
        """Fetch a state object from the manager.

        Raises
        ------
        KeyError
            When the user_id or session_id
            aren't found in the store.
        """
        return self.states[user_id][session_id]

    def fetch_raw(self, session_id: str) -> GatewayState:
        """Fetch a single state given the Session ID."""
        return self.states_raw[session_id]

    def remove(self, state):
        """Remove a state from the registry"""
        if not state:
            return

        try:
            self.states_raw.pop(state.session_id)
        except KeyError:
            pass

        try:
            log.debug('removing state: {!r}', state)
            self.states[state.user_id].pop(state.session_id)
        except KeyError:
            pass

    def fetch_states(self, user_id: int, guild_id: int) -> List[GatewayState]:
        """Fetch all states that are tied to a guild."""
        states = []

        for state in self.states[user_id].values():
            # find out if we are the shard for the guild id
            # this works if shard_count == 1 (the default for
            # single gw connections) since N % 1 is always 0
            shard_id = (guild_id >> 22) % state.shard_count

            if shard_id == state.current_shard:
                states.append(state)

        return states

    def user_states(self, user_id: int) -> List[GatewayState]:
        """Fetch all states tied to a single user."""
        return list(self.states[user_id].values())

    def guild_states(self, member_ids: List[int],
                     guild_id: int) -> List[GatewayState]:
        """Fetch all possible states about members in a guild."""
        states = []

        for member_id in member_ids:
            member_states = self.fetch_states(member_id, guild_id)

            # member_states is empty if the user never logged in
            # since server start, so we need to add a dummy state
            if not member_states:
                dummy_state = GatewayState(
                    session_id='',
                    user_id=member_id,
                    presence={
                        'afk': False,
                        'status': 'offline',
                        'game': None,
                        'since': 0
                    }
                )

                states.append(dummy_state)
                continue

            # push all available member states to the result
            # array
            states.extend(member_states)

        return states

    async def shutdown_single(self, state: GatewayState):
        """Send OP Reconnect to a single connection."""
        websocket = state.ws

        await websocket.send({
            'op': OP.RECONNECT
        })

        # wait 200ms
        # so that the client has time to process
        # our payload then close the connection
        await asyncio.sleep(0.2)

        try:
            # try to close the connection ourselves
            await websocket.ws.close(
                code=4000,
                reason='discord shutting down'
            )
        except ConnectionClosed:
            log.info('client {} already closed', state)

    def gen_close_tasks(self):
        """Generate the tasks that will order the clients
        to reconnect.

        This is required to be ran before :meth:`StateManager.close`,
        since this function doesn't wait for the tasks to complete.
        """

        self.accept_new = False

        #: store the shutdown tasks
        tasks = []

        for state in self.states_raw.values():
            if not state.ws:
                continue

            tasks.append(
                self.shutdown_single(state)
            )

        log.info('made {} shutdown tasks', len(tasks))

        return tasks

    def close(self):
        """Close the state manager."""
        self.closed = True
