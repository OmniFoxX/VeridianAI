# terminal_ux.py — OracleAI BitChat terminal UX layer
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class ChatMode:
    """Base class for chat modes"""
    pass


@dataclass
class Public(ChatMode):
    """Public chat mode"""
    pass


@dataclass
class Channel(ChatMode):
    """Channel chat mode"""
    name: str


@dataclass
class PrivateDM(ChatMode):
    """Private DM mode"""
    nickname: str
    peer_id: str


class ChatContext:
    def __init__(self):
        self.current_mode: ChatMode = Public()
        self.active_channels: List[str] = []
        self.active_dms: Dict[str, str] = {}  # nickname -> peer_id
        self.last_private_sender: Optional[Tuple[str, str]] = None

    def format_prompt(self) -> str:
        if isinstance(self.current_mode, Public):
            return "[Public]"
        elif isinstance(self.current_mode, Channel):
            return f"[{self.current_mode.name}]"
        elif isinstance(self.current_mode, PrivateDM):
            return f"[DM: {self.current_mode.nickname}]"
        return ">"

    def get_status_line(self) -> str:
        parts = [" Public"]

        for i, channel in enumerate(self.active_channels):
            parts.append(f"[{i + 2}] {channel}")

        dm_start = 2 + len(self.active_channels)
        for i, (nick, _) in enumerate(self.active_dms.items()):
            parts.append(f"[{i + dm_start}] DM:{nick}")

        return f"Active: {' '.join(parts)}"

    def switch_to_number(self, num: int) -> bool:
        if num == 1:
            self.current_mode = Public()
            print("\033[90m─────────────────────────\033[0m")
            print("\033[90m» Switched to Public chat. Just type to send messages.\033[0m")
            return True

        channel_end = 1 + len(self.active_channels)
        if 1 < num <= channel_end:
            channel_idx = num - 2
            if channel_idx < len(self.active_channels):
                channel = self.active_channels[channel_idx]
                self.current_mode = Channel(channel)
                print("\033[90m─────────────────────────\033[0m")
                print(f"\033[90m» Switched to channel {channel}\033[0m")
                return True

        dm_start = channel_end + 1
        dm_idx = num - dm_start
        dm_list = list(self.active_dms.items())
        if 0 <= dm_idx < len(dm_list):
            nick, peer_id = dm_list[dm_idx]
            self.current_mode = PrivateDM(nick, peer_id)
            print("\033[90m─────────────────────────\033[0m")
            print(f"\033[90m» Switched to DM with {nick}. Just type to send messages.\033[0m")
            return True

        return False

    def add_channel(self, channel: str):
        if channel not in self.active_channels:
            self.active_channels.append(channel)

    def add_dm(self, nickname: str, peer_id: str):
        self.active_dms[nickname] = peer_id

    def enter_dm_mode(self, nickname: str, peer_id: str):
        self.add_dm(nickname, peer_id)
        self.current_mode = PrivateDM(nickname, peer_id)
        print("\033[90m─────────────────────────\033[0m")
        print(f"\033[90m» Entered DM mode with {nickname}. Just type to send messages.\033[0m")

    def switch_to_channel(self, channel: str):
        self.add_channel(channel)
        self.current_mode = Channel(channel)
        print("\033[90m─────────────────────────\033[0m")
        print(f"\033[90m» Switched to channel {channel}\033[0m")

    def switch_to_channel_silent(self, channel: str):
        """Switch to channel without printing — used by gateway outbound routing."""
        self.add_channel(channel)
        self.current_mode = Channel(channel)

    def switch_to_public(self):
        self.current_mode = Public()
        print("\033[90m─────────────────────────\033[0m")
        print("\033[90m» Switched to Public chat. Just type to send messages.\033[0m")

    def join_channel(self, channel: str):
        """Add channel to active_channels if not already present. No mode switch, no print."""
        if channel not in self.active_channels:
            self.active_channels.append(channel)

    def leave_channel(self, channel: str):
        """
        Remove channel from active_channels.
        Does NOT switch current_mode — the caller is responsible for that.
        """
        if channel in self.active_channels:
            self.active_channels.remove(channel)

    def enter_channel_mode(self, channel: str):
        """
        Add channel to active_channels if not present, set current_mode to
        Channel(channel), and print the standard separator + switch message.
        Distinct from switch_to_channel() — both are called from different
        places in bitchat.py and must both exist.
        """
        self.add_channel(channel)
        self.current_mode = Channel(channel)
        print("\033[90m─────────────────────────\033[0m")
        print(f"\033[90m» Entered channel {channel}. Just type to send messages.\033[0m")

    def remove_channel(self, channel: str):
        if channel in self.active_channels:
            self.active_channels.remove(channel)

    def show_conversation_list(self):
        print("\n╭─── Active Conversations ───╮")
        print("│                            │")

        indicator = "→" if isinstance(self.current_mode, Public) else " "
        print(f"│ {indicator}  Public              │")

        num = 2
        for channel in self.active_channels:
            is_current = isinstance(self.current_mode, Channel) and self.current_mode.name == channel
            indicator = "→" if is_current else " "
            padding = " " * (18 - len(channel))
            print(f"│ {indicator} [{num}] {channel}{padding}│")
            num += 1

        for nick, _ in self.active_dms.items():
            is_current = isinstance(self.current_mode, PrivateDM) and self.current_mode.nickname == nick
            indicator = "→" if is_current else " "
            dm_text = f"DM: {nick}"
            padding = " " * (18 - len(dm_text))
            print(f"│ {indicator} [{num}] {dm_text}{padding}│")
            num += 1

        print("│                            │")
        print("╰────────────────────────────╯")

    def get_conversation_list_with_numbers(self) -> str:
        output = "╭─── Select Conversation ───╮\n"

        output += "│  1. Public                │\n"

        num = 2
        for channel in self.active_channels:
            padding = " " * (20 - len(channel))
            output += f"│  {num}. {channel}{padding}│\n"
            num += 1

        for nick, _ in self.active_dms.items():
            dm_text = f"DM: {nick}"
            padding = " " * (20 - len(dm_text))
            output += f"│  {num}. {dm_text}{padding}│\n"
            num += 1

        output += "╰───────────────────────────╯"
        return output


def format_message_display(
    timestamp: datetime,
    sender: str,
    content: str,
    is_private: bool,
    is_channel: bool,
    channel_name: Optional[str],
    recipient: Optional[str],
    my_nickname: str
) -> str:
    """Format a message for display"""
    time_str = timestamp.strftime("%H:%M")

    if is_private:
        if sender == my_nickname:
            if recipient:
                return f"\033[2;38;5;208m[{time_str}|DM]\033[0m \033[38;5;214m<you → {recipient}>\033[0m {content}"
            else:
                return f"\033[2;38;5;208m[{time_str}|DM]\033[0m \033[38;5;214m<you → ???>\033[0m {content}"
        else:
            return f"\033[2;38;5;208m[{time_str}|DM]\033[0m \033[38;5;208m<{sender} → you>\033[0m {content}"
    elif is_channel:
        if sender == my_nickname:
            if channel_name:
                return f"\033[2;34m[{time_str}|{channel_name}]\033[0m \033[38;5;117m<{sender} @ {channel_name}>\033[0m {content}"
            else:
                return f"\033[2;34m[{time_str}|Ch]\033[0m \033[38;5;117m<{sender} @ ???>\033[0m {content}"
        else:
            if channel_name:
                return f"\033[2;34m[{time_str}|{channel_name}]\033[0m \033[34m<{sender} @ {channel_name}>\033[0m {content}"
            else:
                return f"\033[2;34m[{time_str}|Ch]\033[0m \033[34m<{sender} @ ???>\033[0m {content}"
    else:
        if sender == my_nickname:
            return f"\033[2;32m[{time_str}]\033[0m \033[38;5;120m<{sender}>\033[0m {content}"
        else:
            return f"\033[2;32m[{time_str}]\033[0m \033[32m<{sender}>\033[0m {content}"


def print_help():
    """Print help menu"""
    print("\n\033[38;5;46m━━━ BitChat Commands ━━━\033[0m\n")

    print("\033[38;5;40m▶ General\033[0m")
    print("  \033[36m/help\033[0m         Show this help menu")
    print("  \033[36m/name\033[0m \033[90m<name>\033[0m  Change your nickname")
    print("  \033[36m/status\033[0m       Show connection info")
    print("  \033[36m/clear\033[0m        Clear the screen")
    print("  \033[36m/exit\033[0m         Quit BitChat\n")

    print("\033[38;5;40m▶ Navigation\033[0m")
    print("  \033[36m1-9\033[0m           Quick switch to conversation")
    print("  \033[36m/list\033[0m         Show all conversations")
    print("  \033[36m/switch\033[0m       Interactive conversation switcher")
    print("  \033[36m/public\033[0m       Go to public chat\n")

    print("\033[38;5;40m▶ Messaging\033[0m")
    print("  \033[90m(type normally to send in current mode)\033[0m")
    print("  \033[36m/dm\033[0m \033[90m<name>\033[0m    Start private conversation")
    print("  \033[36m/dm\033[0m \033[90m<name> <msg>\033[0m Send quick private message")
    print("  \033[36m/reply\033[0m        Reply to last private message\n")

    print("\033[38;5;40m▶ Channels\033[0m")
    print("  \033[36m/j\033[0m \033[90m#channel\033[0m   Join or create a channel")
    print("  \033[36m/j\033[0m \033[90m#channel <password>\033[0m Join with password")
    print("  \033[36m/leave\033[0m        Leave current channel")
    print("  \033[36m/pass\033[0m \033[90m<pwd>\033[0m   Set channel password (owner only)")
    print("  \033[36m/transfer\033[0m \033[90m@user\033[0m Transfer ownership (owner only)\n")

    print("\033[38;5;40m▶ Discovery\033[0m")
    print("  \033[36m/channels\033[0m     List all discovered channels")
    print("  \033[36m/online\033[0m       Show who's online")
    print("  \033[36m/w\033[0m            Alias for /online\n")

    print("\033[38;5;40m▶ Privacy & Security\033[0m")
    print("  \033[36m/block\033[0m \033[90m@user\033[0m  Block a user")
    print("  \033[36m/block\033[0m        List blocked users")
    print("  \033[36m/unblock\033[0m \033[90m@user\033[0m Unblock a user\n")

    print("\033[38;5;40m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m")


def clear_screen():
    """Clear the terminal screen"""
    print("\033[2J\033[1;1H", end='')


# Export classes
__all__ = [
    'ChatMode', 'Public', 'Channel', 'PrivateDM', 'ChatContext',
    'format_message_display', 'print_help', 'clear_screen',
]