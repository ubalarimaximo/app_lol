import requests
import json
import os
import re
import sys
import time
import threading
from typing import Dict, List, Optional, Tuple
from io import BytesIO
from PIL import Image

# Cuando el app corre como .exe (PyInstaller), sys.executable apunta al .exe.
# En modo script, subimos un nivel desde src/ para llegar a la raíz del proyecto.
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CACHE_DIR = os.path.join(_APP_DIR, "cache")
ICONS_DIR = os.path.join(CACHE_DIR, "icons")

_CACHE_TTL_CHAMPIONS = 3600 * 24   # 24 h — datos estáticos
_CACHE_TTL_TIER      = 3600 * 6    # 6 h  — tier list

_ROLE_ALIASES: Dict[str, str] = {
    "top":     "top",
    "jungle":  "jungle",
    "jng":     "jungle",
    "middle":  "mid",
    "mid":     "mid",
    "bottom":  "bot",
    "bot":     "bot",
    "adc":     "bot",
    "utility": "support",
    "support": "support",
    "sup":     "support",
}

_TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}

# (role_key, lolalytics_lane)
_ROLE_LANES = [
    ("top",     "top"),
    ("jungle",  "jungle"),
    ("mid",     "mid"),
    ("bot",     "adc"),
    ("support", "support"),
]


_OPGG_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    # Omit Accept-Encoding: brotli responses are binary and requests can't decode them
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _cache_valid(path: str, ttl: float) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl


class DataManager:
    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.makedirs(ICONS_DIR, exist_ok=True)

        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        self._patch: str = ""
        # id (int) → clave Data Dragon, ej. "Ahri", "LeeSin"
        self._champ_key:  Dict[int, str] = {}
        # id (int) → nombre para mostrar, ej. "Ahri", "Lee Sin"
        self._champ_name: Dict[int, str] = {}
        # clave → id
        self._key_to_id:  Dict[str, int] = {}

        # role → [{id, key, name, tier, winRate}, ...]  (ordenado por tier/wr)
        self._tier_data: Dict[str, List[Dict]] = {}

        # enemy_cid → [counter_cid, ...] en orden (mejor counter primero)
        self._counter_cache: Dict[int, List[int]] = {}
        # my_cid → [synergy_cid, ...] en orden (mejor sinergia primero)
        self._synergy_cache: Dict[int, List[int]] = {}

        self._initialized = False
        self.on_ready: Optional[callable] = None

    # ------------------------------------------------------------------ #
    #  Inicialización asíncrona                                            #
    # ------------------------------------------------------------------ #

    def initialize_async(self, on_ready: Optional[callable] = None):
        self.on_ready = on_ready
        threading.Thread(target=self._init_worker, daemon=True).start()

    def _init_worker(self):
        self._load_champions()
        self._load_tier_data()
        self._preload_icons()
        self._initialized = True
        if self.on_ready:
            self.on_ready()

    # ------------------------------------------------------------------ #
    #  Carga de campeones (Data Dragon)                                    #
    # ------------------------------------------------------------------ #

    def _load_champions(self):
        cache = os.path.join(CACHE_DIR, "champions.json")

        if _cache_valid(cache, _CACHE_TTL_CHAMPIONS):
            try:
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
                self._patch = data["version"]
                for c in data["champions"]:
                    cid = c["id"]
                    self._champ_key[cid]  = c["key"]
                    self._champ_name[cid] = c["name"]
                    self._key_to_id[c["key"]] = cid
                return
            except Exception:
                pass

        try:
            versions = self._session.get(
                "https://ddragon.leagueoflegends.com/api/versions.json", timeout=10
            ).json()
            self._patch = versions[0]

            url = (
                f"https://ddragon.leagueoflegends.com/cdn/"
                f"{self._patch}/data/en_US/champion.json"
            )
            raw = self._session.get(url, timeout=15).json()

            champions_list = []
            for key, champ in raw["data"].items():
                cid  = int(champ["key"])
                name = champ["name"]
                self._champ_key[cid]  = key
                self._champ_name[cid] = name
                self._key_to_id[key]  = cid
                champions_list.append({"id": cid, "key": key, "name": name})

            with open(cache, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": self._patch, "champions": champions_list},
                    f, ensure_ascii=False,
                )
        except Exception as e:
            print(f"[DataManager] champions load error: {e}")

    # ------------------------------------------------------------------ #
    #  Carga de tier list (lolalytics)                                     #
    # ------------------------------------------------------------------ #

    def _load_tier_data(self):
        cache = os.path.join(CACHE_DIR, "tier_data.json")

        if _cache_valid(cache, _CACHE_TTL_TIER):
            try:
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    self._tier_data = data
                    return
            except Exception:
                pass

        self._tier_data = {}
        try:
            self._fetch_opgg(cache)
        except Exception as e:
            print(f"[DataManager] op.gg error: {e}")

        if not self._tier_data:
            try:
                self._fetch_lolalytics(cache)
            except Exception as e:
                print(f"[DataManager] lolalytics fallback error: {e}")

    _OPGG_POSITIONS = [
        ("top",     "top"),
        ("jungle",  "jungle"),
        ("mid",     "mid"),
        ("bot",     "adc"),
        ("support", "support"),
    ]

    def _fetch_opgg(self, cache_path: str):
        key_re = re.compile(r'/champion/([A-Za-z0-9]+)\.(?:png|webp)')
        total  = 0

        for role, position in self._OPGG_POSITIONS:
            try:
                resp = self._session.get(
                    f"https://www.op.gg/lol/champions?position={position}",
                    headers=_OPGG_HEADERS,
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue

                # Dividir en chunks por cada link de campeón (mantiene el orden de tier)
                parts = re.split(
                    r'(?=href="/lol/champions/[^/]+/build/)', resp.text
                )
                seen: set = set()
                ordered: List[int] = []

                for part in parts:
                    if not part.startswith('href="/lol/champions/'):
                        continue
                    key_m = key_re.search(part[:1500])
                    if not key_m:
                        continue
                    cid = self._key_to_id.get(key_m.group(1))
                    if cid and cid not in seen:
                        seen.add(cid)
                        ordered.append(cid)

                n = len(ordered)
                for i, cid in enumerate(ordered):
                    pct = i / n if n else 0
                    if   pct < 0.15: tier = "S"
                    elif pct < 0.40: tier = "A"
                    elif pct < 0.65: tier = "B"
                    elif pct < 0.85: tier = "C"
                    else:            tier = "D"

                    self._tier_data.setdefault(role, []).append({
                        "id":      cid,
                        "key":     self._champ_key.get(cid, ""),
                        "name":    self._champ_name.get(cid, ""),
                        "tier":    tier,
                        "winRate": 0.0,
                    })
                total += n
                print(f"[DataManager] op.gg {role}: {n} campeones")

            except Exception as e:
                print(f"[DataManager] op.gg {role}: {e}")

        if self._tier_data:
            print(f"[DataManager] op.gg total: {total} entradas")
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(self._tier_data, f)

    def _fetch_lolalytics(self, cache_path: str):
        base = "https://lolalytics.com/lol/tierlist/"

        # Primero: intentar páginas por rol (cada una tiene ~30-50 campeones)
        for role, lane in _ROLE_LANES:
            for url in (f"{base}?lane={lane}", f"{base}{lane}/"):
                try:
                    resp = self._session.get(url, timeout=20)
                    if resp.status_code != 200:
                        continue
                    match = re.search(
                        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                        resp.text, re.DOTALL,
                    )
                    if not match:
                        continue
                    raw = json.loads(match.group(1))
                    champ_list = self._find_champion_list(raw)
                    if champ_list:
                        for entry in champ_list:
                            entry.setdefault("role", role)
                        self._parse_tier_entries(champ_list)
                        break
                except Exception as e:
                    print(f"[DataManager] lolalytics {role} ({url}): {e}")

        # Fallback: página general si no hubo datos por rol
        if not self._tier_data:
            try:
                resp = self._session.get(base, timeout=20)
                match = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    resp.text, re.DOTALL,
                )
                if match:
                    raw = json.loads(match.group(1))
                    champ_list = self._find_champion_list(raw)
                    if champ_list:
                        self._parse_tier_entries(champ_list)
            except Exception as e:
                print(f"[DataManager] lolalytics main: {e}")

        if self._tier_data:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(self._tier_data, f)

    def _find_champion_list(self, obj, depth: int = 0) -> Optional[List]:
        if depth > 15:
            return None
        if isinstance(obj, list) and len(obj) >= 20:
            if obj and isinstance(obj[0], dict):
                keys = set(obj[0].keys())
                if keys & {
                    "winRate", "win_rate", "wr", "win",
                    "tier", "score", "pickRate", "pick_rate",
                    "games", "kda", "banRate",
                }:
                    return obj
        if isinstance(obj, dict):
            for v in obj.values():
                result = self._find_champion_list(v, depth + 1)
                if result:
                    return result
        return None

    def _parse_tier_entries(self, entries: List[Dict]):
        for entry in entries:
            role_raw = (
                entry.get("role") or entry.get("position") or ""
            ).lower()
            role = _ROLE_ALIASES.get(role_raw, role_raw)

            cid = entry.get("id") or entry.get("champion_id") or entry.get("championId")
            if not cid:
                name_raw = (
                    entry.get("name") or entry.get("championName")
                    or entry.get("champion_name") or entry.get("key") or ""
                )
                cid = self._key_to_id.get(name_raw)
                if not cid:
                    cid = next(
                        (v for k, v in self._key_to_id.items()
                         if k.lower() == name_raw.lower()),
                        None,
                    )
            if not cid or not role:
                continue

            wr = float(
                entry.get("winRate") or entry.get("win_rate")
                or entry.get("wr") or entry.get("win") or 0.0
            )
            if 0 < wr < 1:  # decimal → porcentaje
                wr = wr * 100

            tier = entry.get("tier") or self._wr_to_tier(wr)

            self._tier_data.setdefault(role, []).append({
                "id":      cid,
                "key":     self._champ_key.get(cid, ""),
                "name":    self._champ_name.get(cid, ""),
                "tier":    tier,
                "winRate": wr,
            })

        # Ordenar por tier → winRate desc
        for role, champs in self._tier_data.items():
            champs.sort(
                key=lambda x: (
                    _TIER_ORDER.get(x["tier"], 9),
                    -x["winRate"],
                )
            )

    @staticmethod
    def _wr_to_tier(wr: float) -> str:
        if wr >= 54: return "S"
        if wr >= 52: return "A"
        if wr >= 50: return "B"
        if wr >= 48: return "C"
        return "D"

    # ------------------------------------------------------------------ #
    #  Iconos                                                              #
    # ------------------------------------------------------------------ #

    def _preload_icons(self):
        """Descarga en background todos los iconos que no estén en caché."""
        for cid, key in list(self._champ_key.items()):
            icon_path = os.path.join(ICONS_DIR, f"{key}.png")
            if not os.path.exists(icon_path):
                self._download_icon(key)

    def _download_icon(self, key: str):
        if not self._patch:
            return
        url = (
            f"https://ddragon.leagueoflegends.com/cdn/"
            f"{self._patch}/img/champion/{key}.png"
        )
        try:
            resp = self._session.get(url, timeout=8)
            if resp.status_code == 200:
                with open(os.path.join(ICONS_DIR, f"{key}.png"), "wb") as f:
                    f.write(resp.content)
        except Exception:
            pass

    def get_champion_icon(
        self, champion_id: int, size: Tuple[int, int] = (40, 40)
    ) -> Optional[Image.Image]:
        key = self._champ_key.get(champion_id, "")
        if not key:
            return None
        icon_path = os.path.join(ICONS_DIR, f"{key}.png")
        if not os.path.exists(icon_path):
            self._download_icon(key)
        try:
            return Image.open(icon_path).resize(size, Image.LANCZOS)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    #  Accesores                                                           #
    # ------------------------------------------------------------------ #

    def get_champion_key(self, cid: int) -> str:
        return self._champ_key.get(cid, "")

    def get_champion_name(self, cid: int) -> str:
        return self._champ_name.get(cid, "?")

    # ------------------------------------------------------------------ #
    #  Recomendaciones                                                     #
    # ------------------------------------------------------------------ #

    def get_recommendations(
        self,
        role: str,
        enemy_ids: List[int],
        ally_ids: List[int],
        top_n: int = 6,
    ) -> List[Dict]:
        normalized = _ROLE_ALIASES.get(role.lower(), role.lower())
        pool = self._tier_data.get(normalized, [])
        excluded = set(enemy_ids) | set(ally_ids)
        result = [c for c in pool if c["id"] not in excluded][:top_n]

        # Si no hay datos para el rol, devuelve los mejores de cualquier rol
        if not result:
            seen: set = set()
            for champs in self._tier_data.values():
                for c in champs:
                    if c["id"] not in excluded and c["id"] not in seen:
                        result.append(c)
                        seen.add(c["id"])
                    if len(result) >= top_n:
                        break
                if len(result) >= top_n:
                    break

        return result

    # ------------------------------------------------------------------ #
    #  Counters dinámicos                                                   #
    # ------------------------------------------------------------------ #

    _OPGG_ROLE_SLUG: Dict[str, str] = {
        "top": "top", "jungle": "jungle",
        "middle": "mid", "mid": "mid",
        "bottom": "adc", "bot": "adc",
        "utility": "support", "support": "support",
    }

    def fetch_counters_for(self, enemy_cid: int, enemy_role: str) -> List[int]:
        """Devuelve IDs de campeones que countean a enemy_cid (mejor primero).
        Usa caché en memoria; la llamada es bloqueante, hacerla en un hilo bg."""
        if enemy_cid in self._counter_cache:
            return self._counter_cache[enemy_cid]

        key = self._champ_key.get(enemy_cid, "")
        if not key:
            return []

        slug     = key.lower()
        role_seg = self._OPGG_ROLE_SLUG.get(enemy_role.lower(), "")
        url      = (
            f"https://www.op.gg/lol/champions/{slug}/counters/{role_seg}"
            if role_seg else
            f"https://www.op.gg/lol/champions/{slug}/counters"
        )
        try:
            resp = self._session.get(url, headers=_OPGG_HEADERS, timeout=10)
            if resp.status_code != 200:
                self._counter_cache[enemy_cid] = []
                return []
            counters = self._parse_opgg_counter_page(resp.text, key)
            self._counter_cache[enemy_cid] = counters
            print(f"[DataManager] counters {key}: {len(counters)} encontrados")
            return counters
        except Exception as e:
            print(f"[DataManager] counters {key}: {e}")
            self._counter_cache[enemy_cid] = []
            return []

    def _parse_opgg_counter_page(self, html: str, enemy_key: str) -> List[int]:
        key_re = re.compile(r'/champion/([A-Za-z0-9]+)\.(?:png|webp)')
        seen: set = set()
        result: List[int] = []
        for m in key_re.finditer(html):
            k = m.group(1)
            if k == enemy_key or k in seen:
                continue
            cid = self._key_to_id.get(k)
            if cid:
                seen.add(k)
                result.append(cid)
        return result[:25]  # top 25 (los primeros son los mejores counters)

    def fetch_synergy_for(self, my_cid: int, my_role: str) -> List[int]:
        """Devuelve IDs de campeones con mejor sinergia con my_cid (mejor primero).
        Usa caché en memoria; la llamada es bloqueante, hacerla en un hilo bg."""
        if my_cid in self._synergy_cache:
            return self._synergy_cache[my_cid]

        key = self._champ_key.get(my_cid, "")
        if not key:
            return []

        slug     = key.lower()
        role_seg = self._OPGG_ROLE_SLUG.get(my_role.lower(), "")
        url      = (
            f"https://www.op.gg/lol/champions/{slug}/with/{role_seg}"
            if role_seg else
            f"https://www.op.gg/lol/champions/{slug}/with"
        )
        try:
            resp = self._session.get(url, headers=_OPGG_HEADERS, timeout=10)
            if resp.status_code != 200:
                self._synergy_cache[my_cid] = []
                return []
            synergy = self._parse_opgg_counter_page(resp.text, key)
            self._synergy_cache[my_cid] = synergy
            print(f"[DataManager] synergy {key}: {len(synergy)} encontrados")
            return synergy
        except Exception as e:
            print(f"[DataManager] synergy {key}: {e}")
            self._synergy_cache[my_cid] = []
            return []

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def patch_version(self) -> str:
        return self._patch
