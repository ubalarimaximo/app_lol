import queue
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageDraw


def _assets_icons_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets" / "icons"
    return Path(__file__).parent.parent / "assets" / "icons"

from .lcu_client import LCUClient, PHASE_LABELS
from .data_manager import DataManager

# ── Tema ──────────────────────────────────────────────────────────────────── #
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C_BG          = "#0a0e17"
C_PANEL       = "#0f1623"
C_CARD        = "#111d2e"
C_ACCENT      = "#1a3a5c"
C_GOLD        = "#c8aa6e"
C_GOLD_BRIGHT = "#f0e2b2"
C_GREEN       = "#1fa848"
C_RED         = "#c0392b"
C_TEXT        = "#d4dae6"
C_MUTED       = "#4d5e7a"
C_BORDER      = "#1c2e45"
C_ALLY        = "#1e5fa0"
C_ENEMY       = "#8b1a1a"

TIER_COLOR = {"S": "#ffd700", "A": "#00e676", "B": "#40c4ff", "C": "#ce93d8", "D": "#90a4ae"}


ROLE_LABEL = {
    "top":     "Top",
    "jungle":  "Jungle",
    "middle":  "Mid",
    "bottom":  "Bot",
    "utility": "Support",
}

ROLE_ICON_FILE = {
    "top":     "top_icon.webp",
    "jungle":  "jg_icon.webp",
    "mid":     "mid_icon.webp",
    "middle":  "mid_icon.webp",
    "bot":     "adc_icon.webp",
    "bottom":  "adc_icon.webp",
    "support": "supp_icon.webp",
    "utility": "supp_icon.webp",
}


class LoLAssistantApp:
    def __init__(self):
        self._root = ctk.CTk()
        self._root.title("LoL Assistant")
        self._root.geometry("960x760")
        self._root.minsize(820, 640)
        self._root.configure(fg_color=C_BG)

        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LoLAssistant")
        except Exception:
            pass

        _ico = _assets_icons_dir() / "app_icon.ico"
        _png = _assets_icons_dir() / "app_icon.png"
        if _ico.exists():
            self._root.iconbitmap(str(_ico))
        elif _png.exists():
            from PIL import ImageTk
            _pil         = Image.open(_png)
            self._app_icon = ImageTk.PhotoImage(_pil)   # evita garbage collection
            self._root.iconphoto(True, self._app_icon)

        self._lcu   = LCUClient()
        self._data  = DataManager()
        self._queue: queue.Queue = queue.Queue()

        self._phase:          str            = "None"
        self._session:        Optional[Dict] = None
        self._session_hash:   int            = 0
        self._icon_cache:     Dict           = {}  # (cid, size) → CTkImage
        self._role_icons:     Dict           = {}  # role str → CTkImage
        self._mastery:        List[Dict]     = []  # maestría del jugador (top N)

        # counters dinámicos
        self._seen_enemy_cids: set           = set()   # picks ya procesados
        self._counter_scores:  Dict[int,int] = {}      # cid → puntos acumulados
        self._synergy_fetched: bool          = False   # ya pedimos sinergia este champ select

        self._load_role_icons()
        self._build_ui()
        self._wire_lcu()

        self._data.initialize_async(on_ready=lambda: self._enqueue("data_ready"))
        self._lcu.start()
        self._drain_queue()

    # ------------------------------------------------------------------ #
    #  Cola de eventos (thread-safe)                                       #
    # ------------------------------------------------------------------ #

    def _enqueue(self, kind: str, payload=None):
        self._queue.put((kind, payload))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._dispatch(kind, payload)
        except queue.Empty:
            pass
        self._root.after(200, self._drain_queue)

    def _dispatch(self, kind: str, payload):
        handlers = {
            "connect":          self._on_connected,
            "disconnect":       self._on_disconnected,
            "match_found":      self._on_match_found,
            "phase_change":     self._on_phase_change,
            "champ_select":     self._on_champ_select,
            "champ_select_end": self._on_champ_select_end,
            "data_ready":       self._on_data_ready,
            "counters_updated": self._on_counters_updated,
            "synergy_ready":    self._on_synergy_ready,
        }
        fn = handlers.get(kind)
        if fn:
            fn(payload) if payload is not None else fn()

    def _wire_lcu(self):
        self._lcu.on_connect             = lambda:   self._enqueue("connect")
        self._lcu.on_disconnect          = lambda:   self._enqueue("disconnect")
        self._lcu.on_match_found         = lambda:   self._enqueue("match_found")
        self._lcu.on_phase_change        = lambda p: self._enqueue("phase_change", p)
        self._lcu.on_champ_select_update = lambda s: self._enqueue("champ_select", s)
        self._lcu.on_champ_select_end    = lambda:   self._enqueue("champ_select_end")

    # ------------------------------------------------------------------ #
    #  Construcción de la UI                                               #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────── #
        hdr = ctk.CTkFrame(self._root, fg_color=C_PANEL, height=64, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        inner = ctk.CTkFrame(hdr, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=24, pady=10)

        ctk.CTkLabel(
            inner, text="⚡  LoL Assistant",
            font=ctk.CTkFont("Segoe UI", 20, "bold"),
            text_color=C_GOLD_BRIGHT,
        ).pack(side="left")

        self._lbl_phase = ctk.CTkLabel(
            inner, text="",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=C_MUTED,
        )
        self._lbl_phase.pack(side="right", padx=14)

        self._lbl_status = ctk.CTkLabel(
            inner, text="● Desconectado",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=C_RED,
        )
        self._lbl_status.pack(side="right")

        # ── Separador dorado ───────────────────────────────────────────── #
        sep = ctk.CTkFrame(self._root, fg_color=C_GOLD, height=2, corner_radius=0)
        sep.pack(fill="x")

        # ── Contenido principal ───────────────────────────────────────── #
        self._content = ctk.CTkFrame(self._root, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=16, pady=12)

        self._show_idle("Esperando el cliente de League of Legends...")

    # ------------------------------------------------------------------ #
    #  Vistas                                                              #
    # ------------------------------------------------------------------ #

    def _clear(self):
        for w in self._content.winfo_children():
            w.destroy()

    # ── Vista idle ────────────────────────────────────────────────────── #
    def _show_idle(self, msg: str, sub: str = ""):
        self._clear()
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        frame.place(relx=0.5, rely=0.45, anchor="center")

        if not hasattr(self, "_idle_icon"):
            _p = _assets_icons_dir() / "app_icon.png"
            if _p.exists():
                _pil = Image.open(_p).convert("RGBA").resize((120, 120), Image.LANCZOS)
                self._idle_icon = ctk.CTkImage(light_image=_pil, dark_image=_pil, size=(120, 120))
            else:
                self._idle_icon = None

        if self._idle_icon:
            ctk.CTkLabel(frame, image=self._idle_icon, text="").pack(pady=(0, 18))
        else:
            ctk.CTkLabel(frame, text="⚡", font=ctk.CTkFont(size=64), text_color=C_GOLD).pack(pady=(0, 14))
        ctk.CTkLabel(
            frame, text=msg,
            font=ctk.CTkFont("Segoe UI", 16),
            text_color=C_TEXT,
        ).pack()
        if sub:
            ctk.CTkLabel(
                frame, text=sub,
                font=ctk.CTkFont("Segoe UI", 11),
                text_color=C_MUTED,
                wraplength=400,
            ).pack(pady=6)

    # ── Vista champion select ──────────────────────────────────────────── #
    def _show_champ_select(self, session: Dict):
        self._clear()

        local_cell  = session.get("localPlayerCellId", -1)
        my_team     = session.get("myTeam", [])
        their_team  = session.get("theirTeam", [])
        bans        = session.get("bans", {})

        my_cell_data = next(
            (c for c in my_team if c.get("cellId") == local_cell), {}
        )
        my_role    = my_cell_data.get("assignedPosition", "")
        my_champ   = my_cell_data.get("championId", 0)

        enemy_ids = [c["championId"] for c in their_team if c.get("championId", 0) > 0]
        ally_ids  = [
            c["championId"] for c in my_team
            if c.get("championId", 0) > 0 and c.get("cellId") != local_cell
        ]

        # Excluir bans y el propio pick del jugador de las recomendaciones.
        # Fuente 1: campo bans (no siempre está completo)
        ban_set: set = {
            b for b in bans.get("myTeamBans", []) + bans.get("theirTeamBans", [])
            if b > 0
        }
        # Fuente 2: array de actions (más confiable — cada ban es una action completada)
        for action_group in session.get("actions", []):
            group = action_group if isinstance(action_group, list) else [action_group]
            for act in group:
                if act.get("type") == "ban" and act.get("completed") and act.get("championId", 0) > 0:
                    ban_set.add(act["championId"])
        if my_champ > 0:
            ban_set.add(my_champ)
        ban_ids = list(ban_set)

        # ── Fila superior: aliados | enemigos ────────────────────────── #
        top = ctk.CTkFrame(self._content, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        self._team_panel(top, "🟦  TU EQUIPO", my_team,    local_cell, col=0)
        self._team_panel(top, "🟥  ENEMIGOS",  their_team, -1,         col=1)

        # ── Bans ──────────────────────────────────────────────────────── #
        self._bans_row(bans, session)

        # ── Panel inferior: scores si todos pickearon, recomendaciones si no ── #
        all_picks_done = (
            my_champ > 0
            and all(c.get("championId", 0) > 0 for c in my_team)
            and all(c.get("championId", 0) > 0 for c in their_team)
        )
        if all_picks_done:
            self._scores_panel(my_champ, my_team, their_team, local_cell)
        else:
            role_display = ROLE_LABEL.get(my_role, my_role).upper() if my_role else "SIN ROL ASIGNADO"
            self._recommendations_panel(my_role, enemy_ids, ally_ids + ban_ids, role_display)

    # ── Panel de equipo ──────────────────────────────────────────────── #
    def _team_panel(
        self, parent, title: str, team: List[Dict], local_cell: int, col: int
    ):
        panel = ctk.CTkFrame(parent, fg_color=C_PANEL, corner_radius=10)
        panel.grid(row=0, column=col, padx=5, sticky="nsew")

        ctk.CTkLabel(
            panel, text=title,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
            text_color=C_GOLD,
        ).pack(padx=14, pady=(10, 6), anchor="w")

        sep = ctk.CTkFrame(panel, fg_color=C_BORDER, height=1, corner_radius=0)
        sep.pack(fill="x", padx=10, pady=(0, 6))

        for cell in team:
            cid      = cell.get("championId", 0)
            cell_id  = cell.get("cellId", -1)
            position = cell.get("assignedPosition", "")
            is_me    = (cell_id == local_cell)

            row = ctk.CTkFrame(
                panel,
                fg_color=C_ACCENT if is_me else "transparent",
                corner_radius=6,
            )
            row.pack(fill="x", padx=8, pady=2)

            # Icono
            img = self._get_icon(cid, (36, 36)) if cid > 0 else self._placeholder_icon((36, 36))
            ctk.CTkLabel(row, image=img, text="", width=36, height=36).pack(
                side="left", padx=(6, 6), pady=4
            )

            # Nombre + rol
            name   = self._data.get_champion_name(cid) if cid > 0 else "— —"
            suffix = "  👤 TÚ" if is_me else ""

            role_img = self._role_icons.get(position)
            if role_img:
                ctk.CTkLabel(row, image=role_img, text="", width=22, height=22).pack(
                    side="left", padx=(2, 4)
                )

            ctk.CTkLabel(
                row,
                text=f"{name}{suffix}",
                font=ctk.CTkFont("Segoe UI", 12, "bold" if is_me else "normal"),
                text_color=C_GOLD if is_me else C_TEXT,
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        ctk.CTkLabel(panel, text="", height=6).pack()

    # ── Fila de bans ─────────────────────────────────────────────────── #
    def _bans_row(self, bans: Dict, session: Optional[Dict] = None):
        my_bans    = [b for b in bans.get("myTeamBans", [])    if b > 0]
        their_bans = [b for b in bans.get("theirTeamBans", []) if b > 0]
        # Fallback: leer del array de actions cuando myTeamBans está vacío
        if not my_bans and not their_bans and session:
            for action_group in session.get("actions", []):
                group = action_group if isinstance(action_group, list) else [action_group]
                for act in group:
                    if act.get("type") == "ban" and act.get("completed") and act.get("championId", 0) > 0:
                        my_bans.append(act["championId"])
        all_bans   = my_bans + their_bans
        if not all_bans:
            return

        frame = ctk.CTkFrame(self._content, fg_color=C_PANEL, corner_radius=8)
        frame.pack(fill="x", pady=(0, 8))

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(padx=12, pady=6)

        ctk.CTkLabel(
            inner, text="BANS:",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            text_color=C_MUTED,
        ).pack(side="left", padx=(0, 6))

        for i, cid in enumerate(all_bans[:10]):
            img = self._get_icon(cid, (26, 26))
            lbl = ctk.CTkLabel(inner, image=img, text="")
            lbl.pack(side="left", padx=2)
            # Separador visual entre bans propios y enemigos
            if i == len(my_bans) - 1 and their_bans:
                ctk.CTkLabel(
                    inner, text="|", text_color=C_BORDER, font=ctk.CTkFont(size=16)
                ).pack(side="left", padx=4)

    # ── Panel de recomendaciones ──────────────────────────────────────── #
    def _recommendations_panel(
        self, role: str, enemy_ids: List[int], ally_ids: List[int], role_display: str
    ):
        # Calcular recomendaciones antes de construir la UI
        recs: List[Dict] = []
        is_mastery = False

        _TIER_RANK = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "": 5}

        if not self._data.initialized:
            pass  # se muestra spinner abajo
        else:
            # Pedir más recs de las necesarias para re-rankear bien
            raw = self._data.get_recommendations(role, enemy_ids, ally_ids, top_n=20)
            recs = [dict(c) for c in raw]  # copias para no mutar el cache

            if self._counter_scores and recs:
                # Re-rankear: tier base ajustado por counter scores
                def _sort_key(c):
                    tr    = _TIER_RANK.get(c.get("tier", ""), 5)
                    boost = min(self._counter_scores.get(c["id"], 0), 3)
                    return (tr - boost, -c.get("winRate", 0))
                recs.sort(key=_sort_key)
                for c in recs:
                    c["counterScore"] = self._counter_scores.get(c["id"], 0)

            recs = recs[:15]

            if not recs:
                recs = self._get_mastery_recs(enemy_ids, ally_ids, top_n=15)
                is_mastery = bool(recs)

        has_counters = bool(self._counter_scores) and not is_mastery

        # Construir UI
        frame = ctk.CTkFrame(self._content, fg_color=C_PANEL, corner_radius=10)
        frame.pack(fill="both", expand=True)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 6))

        source      = "TUS FAVORITOS" if is_mastery else "RECOMENDADOS"
        counter_tag = "  ⚔ vs enemigos" if has_counters else ""
        role_label  = ROLE_LABEL.get(role, role).upper() if role else "SIN ROL ASIGNADO"

        ctk.CTkLabel(
            hdr,
            text=f"{source}{counter_tag}  —  ",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=C_GOLD_BRIGHT,
        ).pack(side="left")

        role_img = self._role_icons.get(role)
        if role_img:
            ctk.CTkLabel(hdr, image=role_img, text="", width=22, height=22).pack(
                side="left", padx=(0, 5)
            )

        ctk.CTkLabel(
            hdr,
            text=role_label,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=C_GOLD_BRIGHT,
        ).pack(side="left")

        sep = ctk.CTkFrame(frame, fg_color=C_BORDER, height=1, corner_radius=0)
        sep.pack(fill="x", padx=10, pady=(0, 10))

        if not self._data.initialized:
            ctk.CTkLabel(
                frame,
                text="Cargando datos de campeones...",
                text_color=C_MUTED,
                font=ctk.CTkFont("Segoe UI", 12),
            ).pack(pady=20)
            return

        if not recs:
            ctk.CTkLabel(
                frame,
                text="Sin recomendaciones. Conectate al cliente para ver tus campeones.",
                text_color=C_MUTED,
                font=ctk.CTkFont("Segoe UI", 12),
                wraplength=600,
            ).pack(pady=20)
            return

        COLS = 5
        cards = ctk.CTkFrame(frame, fg_color="transparent")
        cards.pack(fill="both", expand=True, padx=10, pady=(0, 12))

        for i, champ in enumerate(recs):
            r, c = divmod(i, COLS)
            self._champ_card(cards, champ, r, c)

    def _get_mastery_recs(
        self, enemy_ids: List[int], ally_ids: List[int], top_n: int = 6
    ) -> List[Dict]:
        excluded = set(enemy_ids) | set(ally_ids)
        result: List[Dict] = []
        for m in self._mastery:
            cid = m.get("championId", 0)
            if cid > 0 and cid not in excluded:
                result.append({
                    "id":            cid,
                    "name":          self._data.get_champion_name(cid),
                    "tier":          "",
                    "winRate":       0.0,
                    "mastery":       m.get("championLevel", 0),
                    "masteryPoints": m.get("championPoints", 0),
                })
            if len(result) >= top_n:
                break
        return result

    # ── Panel de scores finales ───────────────────────────────────────── #

    @staticmethod
    def _rank_score(ranked_list: List[int], cid: int) -> float:
        try:
            rank = ranked_list.index(cid)
            return round(5 * (1 - rank / 25), 1)
        except ValueError:
            return 0.0

    def _scores_panel(
        self, my_cid: int, my_team: List[Dict],
        their_team: List[Dict], local_cell: int,
    ):
        frame = ctk.CTkFrame(self._content, fg_color=C_PANEL, corner_radius=12)
        frame.pack(fill="both", expand=True)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(
            hdr, text="ANÁLISIS FINAL",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=C_GOLD_BRIGHT,
        ).pack(side="left")

        sep = ctk.CTkFrame(frame, fg_color=C_BORDER, height=1, corner_radius=0)
        sep.pack(fill="x", padx=10, pady=(0, 14))

        # Sinergia: cómo va mi campeón con cada aliado
        synergy_list = self._data._synergy_cache.get(my_cid, [])
        ally_scores  = [
            (c["championId"], self._rank_score(synergy_list, c["championId"]))
            for c in my_team
            if c.get("championId", 0) > 0 and c.get("cellId") != local_cell
        ]
        syn_avg = round(sum(s for _, s in ally_scores) / len(ally_scores), 1) if ally_scores else 0.0
        self._score_row(frame, "Synergy Average", syn_avg, ally_scores)

        ctk.CTkFrame(frame, fg_color=C_BORDER, height=1, corner_radius=0).pack(
            fill="x", padx=10, pady=(6, 12)
        )

        # Counter: cómo countea mi campeón a cada enemigo
        enemy_scores = [
            (
                c["championId"],
                self._rank_score(
                    self._data._counter_cache.get(c["championId"], []), my_cid
                ),
            )
            for c in their_team if c.get("championId", 0) > 0
        ]
        cnt_avg = round(sum(s for _, s in enemy_scores) / len(enemy_scores), 1) if enemy_scores else 0.0
        self._score_row(frame, "Counter Average", cnt_avg, enemy_scores)

    def _score_row(
        self, parent, label: str, avg: float, scores: List[Tuple[int, float]]
    ):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 4))

        # Encabezado: label + promedio
        lbl_frame = ctk.CTkFrame(row, fg_color="transparent")
        lbl_frame.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            lbl_frame, text=label,
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            text_color=C_TEXT,
        ).pack(side="left")

        avg_color = C_GREEN if avg >= 3.0 else (C_GOLD if avg >= 1.5 else C_MUTED)
        ctk.CTkLabel(
            lbl_frame, text=f"  {avg:.1f}",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=avg_color,
        ).pack(side="left")

        # Iconos con puntaje debajo
        champs = ctk.CTkFrame(row, fg_color="transparent")
        champs.pack(anchor="w")

        for cid, score in scores:
            cell = ctk.CTkFrame(champs, fg_color="transparent")
            cell.pack(side="left", padx=8)

            img = self._get_icon(cid, (48, 48))
            ctk.CTkLabel(cell, image=img, text="").pack()

            score_color = C_GREEN if score >= 3.5 else (C_GOLD if score >= 2.0 else C_MUTED)
            ctk.CTkLabel(
                cell, text=f"{score:.1f}",
                font=ctk.CTkFont("Segoe UI", 10, "bold"),
                text_color=score_color,
            ).pack(pady=(2, 0))

    # ── Tarjeta de campeón ────────────────────────────────────────────── #
    def _champ_card(self, parent, champ: Dict, row: int, col: int):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=8, width=155, height=118)
        card.grid(row=row, column=col, padx=5, pady=5, sticky="n")
        card.grid_propagate(False)

        counter_score = champ.get("counterScore", 0)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", pady=(7, 0))

        img = self._get_icon(champ["id"], (52, 52))
        ctk.CTkLabel(top, image=img, text="").pack(side="left", padx=(8, 4))

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="left", fill="y", pady=2)

        tier    = champ.get("tier", "")
        wr      = champ.get("winRate", 0.0)
        mastery = champ.get("mastery", 0)
        pts     = champ.get("masteryPoints", 0)

        if tier:
            tier_frame = ctk.CTkFrame(right, fg_color=TIER_COLOR.get(tier, C_MUTED), corner_radius=4, width=32, height=22)
            tier_frame.pack(anchor="w", pady=(2, 2))
            tier_frame.pack_propagate(False)
            ctk.CTkLabel(
                tier_frame,
                text=tier,
                font=ctk.CTkFont("Segoe UI", 12, "bold"),
                text_color=C_BG,
            ).place(relx=0.5, rely=0.5, anchor="center")
        elif mastery:
            M_COLOR = {7: C_GOLD, 6: "#b84dff", 5: "#40c4ff"}
            ctk.CTkLabel(
                right,
                text=f"M{mastery}",
                font=ctk.CTkFont("Segoe UI", 11, "bold"),
                text_color=M_COLOR.get(mastery, C_MUTED),
            ).pack(anchor="w", pady=(2, 1))

        if wr:
            wr_color = C_GREEN if wr >= 52 else (C_GOLD if wr >= 50 else C_RED)
            ctk.CTkLabel(
                right,
                text=f"{wr:.1f}%",
                font=ctk.CTkFont("Segoe UI", 10, "bold"),
                text_color=wr_color,
            ).pack(anchor="w")
        elif pts:
            pts_str = f"{pts // 1000}k" if pts >= 1000 else str(pts)
            ctk.CTkLabel(
                right,
                text=pts_str,
                font=ctk.CTkFont("Segoe UI", 10),
                text_color=C_MUTED,
            ).pack(anchor="w")

        if counter_score:
            ctk.CTkLabel(
                right,
                text="⚔ COUNTER",
                font=ctk.CTkFont("Segoe UI", 8, "bold"),
                text_color=C_GOLD,
            ).pack(anchor="w")

        name = champ.get("name", "?")
        if len(name) > 13:
            name = name[:12] + "…"
        ctk.CTkLabel(
            card,
            text=name,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
            text_color=C_TEXT,
        ).pack(pady=(3, 4))

    # ------------------------------------------------------------------ #
    #  Iconos                                                              #
    # ------------------------------------------------------------------ #

    def _load_role_icons(self, size: Tuple[int, int] = (22, 22)):
        assets = _assets_icons_dir()
        cache: Dict = {}
        for role, filename in ROLE_ICON_FILE.items():
            if filename in cache:
                self._role_icons[role] = cache[filename]
                continue
            path = assets / filename
            if not path.exists():
                continue
            try:
                pil = self._make_role_icon_pil(path, size)
                img = ctk.CTkImage(light_image=pil, dark_image=pil, size=size)
                self._role_icons[role] = img
                cache[filename] = img
            except Exception:
                pass

    @staticmethod
    def _make_role_icon_pil(path: Path, size: Tuple[int, int]) -> Image.Image:
        img = Image.open(path).convert("RGBA")
        img = img.resize(size, Image.LANCZOS)
        alpha = img.getchannel("A")
        if min(alpha.getdata()) > 200:
            img.putdata([
                (r, g, b, 0) if r > 220 and g > 220 and b > 220 else (r, g, b, 255)
                for r, g, b, _ in img.getdata()
            ])
        return img

    def _get_icon(self, cid: int, size: Tuple[int, int]) -> ctk.CTkImage:
        key = (cid, size)
        if key not in self._icon_cache:
            pil = self._data.get_champion_icon(cid, size)
            if pil is None:
                pil = self._make_placeholder_pil(size)
            self._icon_cache[key] = ctk.CTkImage(
                light_image=pil, dark_image=pil, size=size
            )
        return self._icon_cache[key]

    def _placeholder_icon(self, size: Tuple[int, int]) -> ctk.CTkImage:
        key = ("placeholder", size)
        if key not in self._icon_cache:
            pil = self._make_placeholder_pil(size)
            self._icon_cache[key] = ctk.CTkImage(
                light_image=pil, dark_image=pil, size=size
            )
        return self._icon_cache[key]

    @staticmethod
    def _make_placeholder_pil(size: Tuple[int, int]) -> Image.Image:
        img  = Image.new("RGB", size, color=(40, 55, 71))
        draw = ImageDraw.Draw(img)
        draw.text(
            (size[0] // 2 - 4, size[1] // 2 - 7), "?",
            fill=(140, 140, 140),
        )
        return img

    # ------------------------------------------------------------------ #
    #  Manejadores de eventos                                              #
    # ------------------------------------------------------------------ #

    def _on_connected(self, _=None):
        self._lbl_status.configure(text="● Conectado", text_color=C_GREEN)
        self._show_idle("Cliente detectado. Esperando partida...")
        import threading
        threading.Thread(target=self._fetch_mastery_bg, daemon=True).start()

    def _fetch_mastery_bg(self):
        mastery = self._lcu.get_champion_mastery(top_n=40)
        if mastery:
            self._mastery = mastery

    def _fetch_counters_bg(self, enemy_cid: int, enemy_role: str):
        counters = self._data.fetch_counters_for(enemy_cid, enemy_role)
        if not counters:
            return
        # Boost decreciente: top 8 → 3pts, 9-16 → 2pts, 17-25 → 1pt
        for rank, cid in enumerate(counters):
            pts = 3 if rank < 8 else (2 if rank < 16 else 1)
            self._counter_scores[cid] = self._counter_scores.get(cid, 0) + pts
        self._enqueue("counters_updated")

    def _fetch_synergy_bg(self, my_cid: int, my_role: str):
        self._data.fetch_synergy_for(my_cid, my_role)
        self._enqueue("synergy_ready")

    def _on_counters_updated(self, _=None):
        if self._session:
            self._show_champ_select(self._session)

    def _on_synergy_ready(self, _=None):
        if self._session:
            self._show_champ_select(self._session)

    def _on_disconnected(self, _=None):
        self._lbl_status.configure(text="● Desconectado", text_color=C_RED)
        self._lbl_phase.configure(text="")
        self._show_idle(
            "Cliente no detectado",
            "Abrí el cliente de League of Legends para empezar.",
        )

    def _on_match_found(self, _=None):
        self._lbl_phase.configure(text="¡Partida aceptada! ✔")

    def _on_phase_change(self, phase: str = "None"):
        self._phase = phase
        label = PHASE_LABELS.get(phase, phase)
        self._lbl_phase.configure(text=label)

        if phase == "ChampSelect":
            return  # lo maneja _on_champ_select

        messages = {
            "InProgress":      ("En partida  🎮", ""),
            "EndOfGame":       ("Partida terminada", ""),
            "WaitingForStats": ("Esperando resultados...", ""),
            "PreEndOfGame":    ("Finalizando partida...", ""),
            "Lobby":           ("En lobby", ""),
            "Matchmaking":     ("Buscando partida...", ""),
            "None":            ("Esperando partida...", ""),
        }
        msg, sub = messages.get(phase, (label, ""))
        self._show_idle(msg, sub)

    def _on_champ_select(self, session: Dict):
        h = hash(
            str(session.get("myTeam", []))
            + str(session.get("theirTeam", []))
            + str(session.get("bans", {}))
        )
        if h == self._session_hash:
            return
        self._session_hash = h
        self._session = session

        # Detectar nuevos picks enemigos y fetchear counters si el user no pickeo aún
        local_cell  = session.get("localPlayerCellId", -1)
        my_team     = session.get("myTeam", [])
        their_team  = session.get("theirTeam", [])
        my_cell     = next((c for c in my_team if c.get("cellId") == local_cell), {})
        user_picked = my_cell.get("championId", 0) > 0

        if not user_picked:
            for enemy in their_team:
                ecid = enemy.get("championId", 0)
                if ecid > 0 and ecid not in self._seen_enemy_cids:
                    self._seen_enemy_cids.add(ecid)
                    erole = enemy.get("assignedPosition", "")
                    threading.Thread(
                        target=self._fetch_counters_bg,
                        args=(ecid, erole),
                        daemon=True,
                    ).start()

        my_champ = my_cell.get("championId", 0)
        my_role  = my_cell.get("assignedPosition", "")
        all_picked = (
            my_champ > 0
            and all(c.get("championId", 0) > 0 for c in my_team)
            and all(c.get("championId", 0) > 0 for c in their_team)
        )
        if all_picked and not self._synergy_fetched:
            self._synergy_fetched = True
            threading.Thread(
                target=self._fetch_synergy_bg,
                args=(my_champ, my_role),
                daemon=True,
            ).start()

        self._show_champ_select(session)

    def _on_champ_select_end(self, _=None):
        self._session          = None
        self._session_hash     = 0
        self._seen_enemy_cids  = set()
        self._counter_scores   = {}
        self._synergy_fetched  = False

    def _on_data_ready(self, _=None):
        if self._phase == "ChampSelect" and self._session:
            self._show_champ_select(self._session)

    # ------------------------------------------------------------------ #
    #  Ciclo principal                                                     #
    # ------------------------------------------------------------------ #

    def run(self):
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.mainloop()

    def _on_close(self):
        self._lcu.stop()
        self._root.destroy()
