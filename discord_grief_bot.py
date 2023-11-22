from __future__ import annotations
import os, sys, json, datetime
from logging import basicConfig, DEBUG, INFO, debug, info, warning, error, critical
try:
    from discord.ext import tasks
    import requests, discord
except ImportError as exc:
    sys.exit(f"Missing a dependency!\n{exc}")


LOOP_FREQUENCY = 2
FORMAT_MAP = {
    "{players}": "players",
    "{slots}": "slots",
    "{hostname}": "hostname",
    "{player_list}": "player_list"
}
DEBUG_MODE = False


class Server:
    """Object representing one Plutonium server"""


    def __init__(self, player_array: dict, message_template: str | None, hostname: str, slots: int) -> None:
        """`player_array` Dict containing data about all players currently connected to the server
    
        `message_template` String representing the structure of the message that'll be send to Discord
    
        `hostname` Name of the server
    
        `slots` Integer representing maximum amount of players on the server"""

        self.hostname: str = hostname
        self.message: str = message_template if message_template is not None else ""
        self.slots: int = slots
        self.players: int = len(player_array)
        self.player_list: str = ", ".join([p["username"] for p in player_array])

        self._player_array: dict = player_array
        self._message_collection: dict = {}


    def verify_message(self, message: any) -> "Server":
        """Verify attribute message against value in the config, override if changed. Returns an instance of self"""

        if isinstance(message, str) and self.message != message:
            self.message = message
        return self


    def check_players(self, new_player_array: int) -> bool:
        """Check if there are any changes between stored player array and new player array. If changes are present, all player related attributes are updated.
        
        Returns True if change is detected, False otherwise"""

        if self._player_array != new_player_array:
            self._player_array = new_player_array
            self.players: int = len(new_player_array)
            self.player_list: str = ", ".join([p["username"] for p in new_player_array])
            return True
        return False


    async def inform_channel(self, channel: discord.channel.TextChannel, remove_if_empty: bool) -> None:
        """Responsible for sending and removing messages, if passed `channel` already has a message, it'll be removed. New message is generated and send instead
        
        `remove_if_empty` flag will stop sending a message if there are 0 players on the server"""

        if channel.id in self._message_collection:
            try:
                await self._message_collection[channel.id].delete()
                self._message_collection.pop(channel.id)
            except discord.Forbidden:
                warning(f"Could not remove message from {channel.name} due to permission error")

        try:
            if remove_if_empty and self.players == 0:
                return
            msg_object: discord.Message = await channel.send(self._prepare_message())
        except discord.Forbidden:
            error(f"Could not send message to {channel.name} due to permission error")
        else:
            self._message_collection[channel.id] = msg_object


    def _prepare_message(self) -> str:
        """Generate message content pairing attribute values against FORMAT_MAP constant. Generated message is returned"""

        message: str = str(self.message)
        for key, attr in FORMAT_MAP.items():
            try:
                value: any = getattr(self, attr)
                if not isinstance(value, (str, int, float)):
                    raise ValueError(f"Attribute {attr} is of type {type(value).__name__} expecting str/int/float")
                value = str(value)
            except (AttributeError, ValueError):
                error(f"Attribute {attr} not in this server object")
            else:
                message = message.replace(key, value)
        return message
    

def initialize_logger() -> None:
    try:
        log_file: str = os.path.join(os.path.dirname(__file__), f"log_{datetime.datetime.now().strftime("%Y-%m")}.log")
        basicConfig(level=DEBUG if DEBUG_MODE else INFO, filename=log_file, encoding="utf-8", filemode="a" if os.path.isfile(log_file) else "w", format="%(asctime)s - %(levelname)s - %(message)s", force=True)
    except Exception as exc:
        print(f"Failed to initialize logfile\n{exc}")


def load_config() -> dict:
    """Load configuration file, it is expected to be placed in the same directory as this script under name `config.json`"""

    config_path: str = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as json_io:
            cfg: dict = json.load(json_io)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
        critical(f"Could not load config file from '{config_path}'", exc_info=True)
        sys.exit(f"Could not load config file from {config_path}")
    else:
        return cfg


def get_token() -> str:
    """Import Discord API token. Importing from `discord_tokens.py` takes precedence, function failover to reading `token` key from config file. If both fail, script is closed"""

    try:
        from discord_tokens import DISCORD_GRIEF_BOT_TOKEN as token
    except ImportError:
        cfg: dict = load_config()
        if "token" in cfg and cfg["token"] is not None:
            info(f"Using Discord token from config file")
            return cfg["token"]
        critical("Could not retrieve Discord token from either discord_tokens.py or config.json")
        sys.exit("Could not retrieve Discord token from either discord_tokens.py or config.json")
    else:
        return token


def get_channels_for_server(client: discord.Client, server_cfg: dict) -> list[discord.channel.TextChannel]:
    """Retrieve ids of the channels the messages should be sent into. Currently only one channel per server"""

    channels = []

    for guild in client.guilds:
        for channel in guild.channels:
            if not isinstance(channel, discord.channel.TextChannel):
                continue

            if "partial_channel" in server_cfg and server_cfg["partial_channel"]:
                if server_cfg["channel"] in channel.name.lower():
                    channels.append(channel)
                    break
            else:
                if server_cfg["channel"] == channel.name:
                    channels.append(channel)
                    break

    return channels


def get_api_response(api_url: str) -> dict:
    """Hit Plutonium server API and serialize the response to a json object
    
    `api_url` url to Plutonium API"""

    response: requests.Response = requests.get(api_url)

    if response.status_code != 200:
        warning(f"Request to {api_url} failed with code {response.status_code}")
        return {}

    try:
        serialized_json: dict = response.json()
    except requests.exceptions.JSONDecodeError:
        warning(f"Response could not be serialized to json object", exc_info=True)
        return {}
    else:
        return serialized_json


def retrieve_player_info_from_api(server_data: dict, server_cfg: dict) -> tuple[dict | None, int | None]:
    """Retrieve information about players on the server from api response
    
    `server_data` Response from Plutonium API in the form of JSON object
    
    `server_cfg` Dict contatining configuration details for a server
    
    Returns a tuple, first element is a dict containing info about all players, 2nd is number of available slots on the server"""


    partial_match: bool = "partial_hostname" in server_cfg and server_cfg["partial_hostname"]
    constraints: dict | bool = False
    if "constraints" in server_cfg and len(server_cfg["constraints"]):
        constraints = server["constraints"]

    for server in server_data:
        server: dict

        if constraints and "games" in constraints and server["game"] not in constraints["games"]:
            continue
        if constraints and "maps" in constraints and server["map"] not in constraints["maps"]:
            continue
        if constraints and "gametype" in constraints and server["gametype"] != constraints["gametype"]:
            continue

        # Did not use or for readability
        if server["ip"] == server_cfg["ip"]:
            return (server["players"], int(server["maxplayers"]))
        elif not partial_match and server["hostname"] == server_cfg["hostname"]:
            return (server["players"], int(server["maxplayers"]))
        elif partial_match and server_cfg["hostname"].lower() in server["hostname"].lower():
            return (server["players"], int(server["maxplayers"]))

    return (None, None)


def get_hostname_from_api(ip: str, server_data: dict) -> str:
    """Retrieve server hostname from Plutonium API response based on IP"""
    for server in server_data:
        if server["ip"] == ip:
            return server["hostname"]
    return ""


@tasks.loop(seconds=LOOP_FREQUENCY)
async def main(client: discord.Client, servers: dict):
    # IO is expensive, but by loading config every time, we save ourselves having to restart the bot every time
    cfg: dict = load_config()

    if "servers" not in cfg:
        critical("No servers specified in the config file")
        sys.exit("No servers specified in the config file!")

    api_response: dict = get_api_response(cfg["pluto_api"] if "pluto_api" in cfg and cfg["pluto_api"] is not None else "https://plutonium.pw/api/servers")
    for server in cfg["servers"]:
        server: dict

        # Server is not marked as finalized -> configuration still in progress
        if "finalized" not in server or not server["finalized"]:
            continue

        # Identifiers are missing, it is required that keys are present and at least one is not null
        if "ip" not in server or "hostname" not in server or (server["ip"] is None and server["hostname"] is None):
            continue

        identifier: str = server["hostname"] if server["hostname"] is not None else server["ip"]
        player_data, slots = retrieve_player_info_from_api(api_response, server)

        if player_data is None or slots is None:
            continue

        update: bool = False
        new: bool = False
        server_object: Server | None = None
        if identifier not in servers:
            hostname: str = server["hostname"] if server["hostname"] is not None else get_hostname_from_api(server["ip"], api_response)
            server_object = Server(player_data, server["message"], hostname, slots)
            update, new = len(player_data) > 0, True
        else:
            server_object = servers[identifier]
            server_object.verify_message(server["message"])
            if server_object.check_players(player_data):
                update = True

        if server_object is not None:
            servers[identifier] = server_object

        if update:
            channels: list[discord.channel.TextChannel] = get_channels_for_server(client, server)
            for channel in channels:
                await server_object.inform_channel(channel, server["msg_remove_if_empty_server"])

    debug("\n")


client: discord.Client = discord.Client(intents=discord.Intents.default())
token: str = get_token()
servers: dict[str, Server] = {}
initialize_logger()

@client.event
async def on_ready():
    main.start(client, servers)

try:
    client.run(token)
except discord.LoginFailure:
    critical("Could not autheniticate to Discord with the provided token")
    sys.exit("Could not autheniticate to Discord with the provided token")
