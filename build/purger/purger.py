import json
import sys
import time
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, cast
from urllib.parse import urlparse

import click
import docker
import requests
import yaml
from matrix_client.errors import MatrixError
from typing_extensions import TypedDict

from raiden.constants import DISCOVERY_DEFAULT_ROOM, Environment, Networks
from raiden.network.transport.matrix import make_room_alias
from raiden.network.transport.matrix.client import GMatrixHttpApi
from raiden.settings import DEFAULT_MATRIX_KNOWN_SERVERS
from raiden.utils.typing import ChainID

SYNAPSE_CONFIG_PATH = "/config/synapse.yaml"
USER_PURGING_THRESHOLD = 2 * 24 * 60 * 60  # 2 days
USER_ACTIVITY_PATH = Path("/config/user_activity.json")


class UserActivityInfo(TypedDict):
    last_update: int
    network_to_users: Dict[str, Dict[str, Any]]


@dataclass(frozen=True)
class RoomInfo:
    room_id: str
    alias: str
    server_name: str

    @property
    def local_room_alias(self) -> str:
        return f"#{self.alias}:{self.server_name}"


@click.command()
@click.argument("server")
@click.option("-c", "--credentials-file", required=True, type=click.File("rt"))
@click.option(
    "--docker-restart-label",
    help="If set, search all containers with given label and, if they're running, "
    "restart them if the federation whilelist has changed.",
)
@click.option(
    "--url-known-federation-servers",
    envvar="URL_KNOWN_FEDERATION_SERVERS",
    default=DEFAULT_MATRIX_KNOWN_SERVERS[Environment.PRODUCTION],
)
def purge(
    server: str,
    credentials_file: TextIO,
    docker_restart_label: Optional[str],
    url_known_federation_servers: str,
) -> None:
    """Purge inactive users from broadcast rooms

    SERVER: matrix synapse server url, e.g.: http://hostname

    All option can be passed through uppercase environment variables prefixed with 'MATRIX_'
    """

    try:
        credentials = json.loads(credentials_file.read())
        username = credentials["username"]
        password = credentials["password"]
    except (JSONDecodeError, UnicodeDecodeError, OSError, KeyError) as ex:
        click.secho(f"Invalid credentials file: {ex}", fg="red")
        sys.exit(1)

    api = GMatrixHttpApi(server)
    try:
        response = api.login(
            "m.login.password", user=username, password=password, device_id="purger"
        )
        api.token = response["access_token"]
    except (MatrixError, KeyError) as ex:
        click.secho(f"Could not log in to server {server}: {ex}")
        sys.exit(1)

    try:
        global_user_activity: UserActivityInfo = {
            "last_update": int(time.time()) - USER_PURGING_THRESHOLD - 1,
            "network_to_users": {},
        }

        try:
            global_user_activity = json.loads(USER_ACTIVITY_PATH.read_text())
        except JSONDecodeError:
            click.secho(f"{USER_ACTIVITY_PATH} is not a valid JSON. Starting with empty list")
        except FileNotFoundError:
            click.secho(f"{USER_ACTIVITY_PATH} not found. Starting with empty list")

        # check if there are new networks to add
        for network in Networks:
            if str(network.value) in global_user_activity["network_to_users"]:
                continue
            global_user_activity["network_to_users"][str(network.value)] = dict()

        new_global_user_activity = run_user_purger(api, global_user_activity)

        # write the updated user activity to file
        USER_ACTIVITY_PATH.write_text(json.dumps(cast(Dict[str, Any], new_global_user_activity)))
    finally:
        if docker_restart_label:
            if not url_known_federation_servers:
                # In case an empty env var is set
                url_known_federation_servers = DEFAULT_MATRIX_KNOWN_SERVERS[Environment.PRODUCTION]
            # fetch remote whiltelist
            try:
                remote_whitelist = json.loads(requests.get(url_known_federation_servers).text)[
                    "all_servers"
                ]
            except (requests.RequestException, JSONDecodeError, KeyError) as ex:
                click.secho(
                    f"Error while fetching whitelist: {ex!r}. "
                    f"Ignoring, containers will be restarted.",
                    err=True,
                )
                # An empty whitelist will cause the container to be restarted
                remote_whitelist = []

            client = docker.from_env()  # pylint: disable=no-member
            for container in client.containers.list():
                if container.attrs["State"]["Status"] != "running" or not container.attrs[
                    "Config"
                ]["Labels"].get(docker_restart_label):
                    continue

                try:
                    # fetch local list from container's synapse config
                    local_whitelist = yaml.safe_load(
                        container.exec_run(["cat", SYNAPSE_CONFIG_PATH]).output
                    )["federation_domain_whitelist"]

                    # if list didn't change, don't proceed to restart container
                    if local_whitelist and remote_whitelist == local_whitelist:
                        continue

                    click.secho(f"Whitelist changed. Restarting. new_list={remote_whitelist!r}")
                except (KeyError, IndexError) as ex:
                    click.secho(
                        f"Error fetching container status: {ex!r}. Restarting anyway.",
                        err=True,
                    )
                # restart container
                container.restart(timeout=30)


def run_user_purger(
    api: GMatrixHttpApi,
    global_user_activity: UserActivityInfo,
) -> UserActivityInfo:
    """
    The user purger mechanism finds inactive users which have been offline
    longer than the threshold time and will delete them. By deactivating them
    with {"erase": True} they get removed from all rooms. Empty rooms will be deleted
    in the database.
    Each server is responsible for its own users

    :param api: Api object to own server
    :param global_user_activity: content of user activity file
    :return: updated list of user activities
    """

    # perform update on user presence for due users
    # receive a list for due users on each network
    due_users = update_user_activity(api, global_user_activity)
    # purge due users form rooms
    purge_inactive_users(api, global_user_activity, due_users)

    return global_user_activity


def update_user_activity(
    api: GMatrixHttpApi,
    global_user_activity: UserActivityInfo,
) -> Dict[str, List[str]]:
    """
    runs update on users' presences which are about to be deleted.
    If they are still overdue they are going to be added to the list
    of users to be deleted. Presence updates are included in the
    global list which is going to be stored in the user_activity_file
    :param api: api to its own server
    :param global_user_activity: content of user_activity_file
    :return: a list of due users to be deleted
    """
    current_time = int(time.time())
    last_user_activity_update = global_user_activity["last_update"]
    network_to_users = global_user_activity["network_to_users"]
    network_to_due_users = dict()
    fetch_new_members = False

    # check if new members have to be fetched
    # new members only have to be fetched every
    # USER_PURGING_THRESHOLD since that is the earliest time
    # new members are able to be deleted. This keeps the load on
    # the server as low as possible
    if last_user_activity_update < current_time - USER_PURGING_THRESHOLD:
        fetch_new_members = True
        global_user_activity["last_update"] = current_time

    for network_key, user_activity in network_to_users.items():
        if fetch_new_members:
            discovery_room = get_discovery_room(api, int(network_key))
            if discovery_room is None:
                click.secho(
                    f"No discovery room found for network {network_key}, skipping.", fg="yellow"
                )
                continue
            _fetch_new_members_for_network(
                api=api,
                user_activity=user_activity,
                discovery_room=discovery_room,
                current_time=current_time,
            )

        network_to_due_users[network_key] = _update_user_activity_for_network(
            api=api, user_activity=user_activity, current_time=current_time
        )

    return network_to_due_users


def get_discovery_room(api: GMatrixHttpApi, network_value: int) -> Optional[RoomInfo]:

    server = urlparse(api.base_url).netloc
    discovery_room_alias = make_room_alias(ChainID(network_value), DISCOVERY_DEFAULT_ROOM)
    local_room_alias = f"#{discovery_room_alias}:{server}"

    try:
        room_id = api.get_room_id(local_room_alias)
        return RoomInfo(room_id, discovery_room_alias, server)
    except MatrixError as ex:
        click.secho(f"Could not find room {discovery_room_alias} with error {ex}")

    return None


def _fetch_new_members_for_network(
    api: GMatrixHttpApi, user_activity: Dict[str, int], discovery_room: RoomInfo, current_time: int
) -> None:
    try:
        response = api._send(
            "GET",
            api_path="/_synapse/admin/v1",
            path=f"/rooms/{discovery_room.room_id}/members",
        )
        server_name = urlparse(api.base_url).netloc
        room_members = [
            member
            for member in response["members"]
            if member.split(":")[1] == server_name and not member.startswith("@admin")
        ]

        # Add new members with an overdue activity time
        # to trigger presence update later
        for user_id in room_members:
            if user_id not in user_activity:
                user_activity[user_id] = current_time - USER_PURGING_THRESHOLD - 1

    except MatrixError as ex:
        click.secho(f"Could not fetch members for {discovery_room.alias} with error {ex}")


def _update_user_activity_for_network(
    api: GMatrixHttpApi, user_activity: Dict[str, int], current_time: int
) -> List[str]:
    deadline = current_time - USER_PURGING_THRESHOLD

    possible_candidates = [
        user_id for user_id, last_seen in user_activity.items() if last_seen < deadline
    ]

    due_users = list()
    click.secho(
        f"Presences of {len(possible_candidates)} users will be fetched due to possible inactivity. This might take a while."
    )
    # presence updates are only run for possible due users.
    # This helps to spread the load on the server as good as possible
    # Since this script runs once a day, every day a couple of users are
    # going to be updated rather than all at once
    for user_id in possible_candidates:
        try:
            response = api.get_presence(user_id)
            # in rare cases there is no last_active_ago sent
            if "last_active_ago" in response:
                last_active_ago = response["last_active_ago"] // 1000
            else:
                last_active_ago = USER_PURGING_THRESHOLD + 1
            presence = response["presence"]
            last_seen = current_time - last_active_ago
            user_activity[user_id] = last_seen
            if user_activity[user_id] < deadline and presence == "offline":
                due_users.append(user_id)

        except MatrixError as ex:
            click.secho(f"Could not fetch user presence of {user_id}: {ex}")
        finally:
            time.sleep(0.1)
    return due_users


def purge_inactive_users(
    api: GMatrixHttpApi,
    global_user_activity: UserActivityInfo,
    network_to_due_users: Dict[str, List[str]],
) -> None:
    for network_key, due_users in network_to_due_users.items():
        click.secho(f"Purging {len(due_users)} inactive users from Chain ID {network_key}")
        _purge_inactive_users_for_network(
            api=api,
            user_activity=global_user_activity["network_to_users"][network_key],
            due_users=due_users,
        )


def _purge_inactive_users_for_network(
    api: GMatrixHttpApi,
    user_activity: Dict[str, int],
    due_users: List[str],
) -> None:
    for user_id in due_users:
        try:
            # delete user account and remove him from the user_activity_file
            last_ago = (int(time.time()) - user_activity[user_id]) / (60 * 60 * 24)
            api._send(
                "POST",
                f"/deactivate/{user_id}",
                content={"erase": True},
                api_path="/_synapse/admin/v1",
            )
            user_activity.pop(user_id, None)
            click.secho(f"{user_id} deleted. Offline for {last_ago} days.")
        except MatrixError as ex:
            click.secho(f"Could not delete user {user_id} with error {ex}")
        finally:
            time.sleep(0.1)


if __name__ == "__main__":
    purge(  # pylint: disable=unexpected-keyword-arg,no-value-for-parameter
        auto_envvar_prefix="MATRIX"
    )
