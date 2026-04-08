import qbittorrentapi
import logging
import time

class QbittorrentAdapter:
    def __init__(self, config):
        """Initializes and authenticates the qBittorrent client."""
        qb_config = config.get('qbittorrent', {})
        self.host = qb_config.get('host', 'localhost')
        self.port = qb_config.get('port', 8080)
        self.username = qb_config.get('username', 'admin')
        self.password = qb_config.get('password', 'adminadmin')

        # Instantiate the client
        self.client = qbittorrentapi.Client(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            REQUESTS_ARGS={
                "proxies": {},
            },
        )
        # qbittorrent-api wraps requests.Session internally; disable inherited proxy env vars there.
        if hasattr(self.client, "_session"):
            self.client._session.trust_env = False
            self.client._session.proxies = {}

        try:
            self.client.auth_log_in()
            print(f"Successfully connected to qBittorrent at {self.host}:{self.port}")
        except qbittorrentapi.LoginFailed as e:
            print(f"CRITICAL: Failed to login to qBittorrent. Check config.json. Error: {e}")
            raise e
        except Exception as e:
            print(f"CRITICAL: Could not reach qBittorrent. Is it running? Error: {e}")
            raise e

    def enqueue(self, torrent_hash, save_path, tags):
        """
        Sends the torrent to qBittorrent.
        Sadpanda API provides the raw info_hash, which qBittorrent can use directly
        to download the metadata via DHT, or we can format it as a magnet link.
        """
        # Format the hash into a standard magnet link
        # Appends the official tracker and a reliable backup tracker to resolve metadata instantly
        tracker_1 = "http%3A%2F%2Fehtracker.org%2Fannounce"
        tracker_2 = "udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
        magnet_uri = f"magnet:?xt=urn:btih:{torrent_hash}&tr={tracker_1}&tr={tracker_2}"

        print(f"Sending to qBittorrent -> Hash: {torrent_hash} | Path: {save_path} | Tags: {tags}")

        try:
            # Tell qBittorrent to download it
            self.client.torrents_add(
                urls=magnet_uri,
                save_path=save_path,
                tags=tags
            )
            return True
        except Exception as e:
            print(f"Failed to add torrent to qBittorrent: {e}")
            return False

    def enqueue_torrent_file(self, torrent_file_path, save_path, tags):
        try:
            self.client.torrents_add(
                torrent_files=[torrent_file_path],
                save_path=save_path,
                tags=tags,
            )
            return True
        except Exception as e:
            print(f"Failed to add torrent file to qBittorrent: {e}")
            return False

    def replace_with_torrent_file(self, torrent_hash, torrent_file_path, save_path, tags):
        try:
            self.client.torrents_delete(hashes=torrent_hash, delete_files=False)
        except Exception:
            pass
        time.sleep(1)
        return self.enqueue_torrent_file(torrent_file_path, save_path, tags)

    def _find_torrent(self, torrent_hash):
        target = str(torrent_hash or "").strip().lower()
        if not target:
            return None

        try:
            torrent_info = self.client.torrents_info(torrent_hashes=torrent_hash)
            if torrent_info:
                return torrent_info[0]
        except TypeError:
            try:
                torrent_info = self.client.torrents_info(hashes=torrent_hash)
                if torrent_info:
                    return torrent_info[0]
            except Exception:
                pass
        except Exception:
            pass

        try:
            all_torrents = self.client.torrents_info()
        except Exception:
            return None

        for torrent in all_torrents:
            candidates = [
                getattr(torrent, "hash", ""),
                getattr(torrent, "infohash_v1", ""),
                getattr(torrent, "infohash_v2", ""),
            ]
            for candidate in candidates:
                if str(candidate or "").strip().lower() == target:
                    return torrent
        return None
    
    def get_status(self, torrent_hash):
        """
        Queries qBittorrent for the specific torrent hash.
        Returns a dictionary with progress and completion status.
        """
        try:
            t = self._find_torrent(torrent_hash)
            if t is None:
                return {"found": False, "progress": 0.0, "is_completed": False, "error": False}

            # Progress is a float from 0.0 to 1.0
            progress = float(t.progress)

            seeding_states = ['uploading', 'stalledUP', 'pausedUP', 'forcedUP']
            seeding = t.state in seeding_states
            completed = seeding or progress >= 1.0

            return {
                "found": True,
                "progress": progress,
                "is_completed": completed,
                "is_seeding": seeding,
                "state": str(t.state),
                "added_epoch": int(getattr(t, "added_on", 0) or 0),
                "error": False,
            }
        except Exception as e:
            print(f"Error checking torrent status from qBittorrent: {e}")
            return {
                "found": False,
                "progress": 0.0,
                "is_completed": False,
                "is_seeding": False,
                "state": "",
                "added_epoch": 0,
                "error": True,
            }
            
    def get_save_path(self, torrent_hash):
        """
        Asks qBittorrent for the absolute path to the downloaded content.
        This resolves exactly where the .zip or folder lives on the hard drive.
        """
        try:
            t = self._find_torrent(torrent_hash)
            if t is None:
                return None
            # 'content_path' gives the exact absolute path to the single file or the root folder
            return t.content_path
        except Exception as e:
            print(f"Error getting save path from qBittorrent: {e}")
            return None
            
# --- Helper function used by your Main Worker ---
def get_adapter(config):
    return QbittorrentAdapter(config)
