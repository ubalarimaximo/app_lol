import psutil
import base64
import requests
import urllib3
import threading
import time
import os
from typing import Optional, Callable, Dict, Any, List

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PHASE_LABELS: Dict[str, str] = {
    "None":            "Sin partida",
    "Lobby":           "En lobby",
    "Matchmaking":     "Buscando partida...",
    "ReadyCheck":      "¡Partida encontrada!",
    "ChampSelect":     "Selección de campeones",
    "GameStart":       "Iniciando partida...",
    "InProgress":      "En partida",
    "WaitingForStats": "Esperando resultados",
    "PreEndOfGame":    "Pre-fin de partida",
    "EndOfGame":       "Fin de partida",
}


class LCUClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.verify = False
        self._port: Optional[int] = None
        self._connected = False
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._last_phase: Optional[str] = None

        # Callbacks — ejecutados desde el hilo de polling; usar root.after() para GUI
        self.on_connect:              Optional[Callable[[], None]]      = None
        self.on_disconnect:           Optional[Callable[[], None]]      = None
        self.on_match_found:          Optional[Callable[[], None]]      = None
        self.on_phase_change:         Optional[Callable[[str], None]]   = None
        self.on_champ_select_update:  Optional[Callable[[Dict], None]]  = None
        self.on_champ_select_end:     Optional[Callable[[], None]]      = None

    # ------------------------------------------------------------------ #
    #  Detección del cliente                                               #
    # ------------------------------------------------------------------ #

    def _connect_from_cmdline(self) -> bool:
        """Lee el puerto y token desde los args del proceso LeagueClientUx.exe."""
        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    if proc.info["name"] != "LeagueClientUx.exe":
                        continue
                    cmdline = proc.info["cmdline"] or []
                    port, token = None, None
                    for arg in cmdline:
                        if "--app-port=" in arg:
                            port = int(arg.split("=", 1)[1])
                        if "--remoting-auth-token=" in arg:
                            token = arg.split("=", 1)[1]
                    if port and token:
                        self._port = port
                        creds = base64.b64encode(f"riot:{token}".encode()).decode()
                        self._session.headers.update({
                            "Authorization": f"Basic {creds}",
                            "Content-Type":  "application/json",
                        })
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        return False

    def _connect_from_lockfile(self) -> bool:
        """Lee el lockfile desde el directorio de instalación de LoL."""
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    if proc.info["name"] != "LeagueClientUx.exe":
                        continue
                    exe = proc.info["exe"]
                    search_dir = os.path.dirname(exe)
                    for _ in range(3):  # sube hasta 3 niveles buscando el lockfile
                        lockfile = os.path.join(search_dir, "lockfile")
                        if os.path.exists(lockfile):
                            with open(lockfile, encoding="utf-8") as f:
                                parts = f.read().strip().split(":")
                            if len(parts) >= 5:
                                self._port = int(parts[2])
                                token = parts[3]
                                creds = base64.b64encode(f"riot:{token}".encode()).decode()
                                self._session.headers.update({
                                    "Authorization": f"Basic {creds}",
                                    "Content-Type":  "application/json",
                                })
                                return True
                        search_dir = os.path.dirname(search_dir)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return False

    def _find_lcu(self) -> bool:
        return self._connect_from_cmdline() or self._connect_from_lockfile()

    # ------------------------------------------------------------------ #
    #  HTTP helpers                                                        #
    # ------------------------------------------------------------------ #

    def _get(self, endpoint: str) -> Optional[Any]:
        try:
            resp = self._session.get(
                f"https://127.0.0.1:{self._port}{endpoint}", timeout=3
            )
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return resp.text
        except Exception:
            pass
        return None

    def _post(self, endpoint: str, json_data=None) -> bool:
        try:
            resp = self._session.post(
                f"https://127.0.0.1:{self._port}{endpoint}",
                json=json_data,
                timeout=3,
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  API pública                                                         #
    # ------------------------------------------------------------------ #

    def accept_match(self) -> bool:
        return self._post("/lol-matchmaking/v1/ready-check/accept")

    def get_champion_mastery(self, top_n: int = 40) -> List[Dict]:
        result = self._get(f"/lol-champion-mastery/v1/champion-mastery/top?count={top_n}")
        return result if isinstance(result, list) else []

    def get_gameflow_phase(self) -> Optional[str]:
        result = self._get("/lol-gameflow/v1/gameflow-phase")
        if isinstance(result, str):
            return result.strip('"')
        return None

    def get_champ_select_session(self) -> Optional[Dict]:
        result = self._get("/lol-champ-select/v1/session")
        return result if isinstance(result, dict) else None

    # ------------------------------------------------------------------ #
    #  Loop de polling                                                     #
    # ------------------------------------------------------------------ #

    def _poll_loop(self):
        while self._running:
            # --- sin conexión: intentar conectar ---
            if not self._connected:
                if self._find_lcu() and self.get_gameflow_phase() is not None:
                    self._connected = True
                    if self.on_connect:
                        self.on_connect()
                time.sleep(2)
                continue

            # --- con conexión: leer fase ---
            phase = self.get_gameflow_phase()

            if phase is None:
                self._connected = False
                self._port = None
                self._last_phase = None
                if self.on_disconnect:
                    self.on_disconnect()
                time.sleep(2)
                continue

            # Notificar cambio de fase
            if phase != self._last_phase and self.on_phase_change:
                self.on_phase_change(phase)

            # Manejar ReadyCheck → auto-aceptar
            if phase == "ReadyCheck":
                rc = self._get("/lol-matchmaking/v1/ready-check")
                if (isinstance(rc, dict)
                        and rc.get("state") == "InProgress"
                        and rc.get("playerResponse") == "None"):
                    self.accept_match()
                    if self.on_match_found:
                        self.on_match_found()

            # Manejar ChampSelect
            elif phase == "ChampSelect":
                session = self.get_champ_select_session()
                if session and self.on_champ_select_update:
                    self.on_champ_select_update(session)

            # Salida de ChampSelect
            if self._last_phase == "ChampSelect" and phase != "ChampSelect":
                if self.on_champ_select_end:
                    self.on_champ_select_end()

            self._last_phase = phase
            time.sleep(1)

    # ------------------------------------------------------------------ #
    #  Control                                                             #
    # ------------------------------------------------------------------ #

    def start(self):
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self):
        self._running = False
