"""
PositionLock — singleton thread-safe para máximo 1 posición activa entre todos los watchers.

El lock vive en memoria del proceso. Todos los watcher threads del mismo proceso
comparten la misma instancia.
"""
import threading
import time
import logging

logger = logging.getLogger(__name__)


class PositionLock:
    _instance = None
    _class_lock = threading.Lock()

    def __init__(self):
        self._mu   = threading.Lock()
        self._data: dict = {}

    @classmethod
    def get(cls) -> "PositionLock":
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = PositionLock()
        return cls._instance

    # ── Estado ───────────────────────────────────────────────────────────────

    def is_free(self) -> bool:
        with self._mu:
            return not bool(self._data)

    def is_mine(self, ticker: str) -> bool:
        """Retorna True si este ticker es el dueño del lock."""
        with self._mu:
            return self._data.get("ticker") == ticker

    # ── Adquirir / liberar ────────────────────────────────────────────────────

    def acquire(
        self,
        ticker: str,
        direction: str,
        position_id: str,
        entry_price: float,
        interval: str = "",
    ) -> bool:
        """
        Intenta adquirir el lock. Retorna True si lo logra, False si ya está ocupado.
        Atómico — seguro para uso concurrente entre watcher threads.
        """
        with self._mu:
            if self._data:
                logger.debug(
                    "[PositionLock] acquire rechazado — ocupado por %s %s",
                    self._data.get("ticker"), self._data.get("direction"),
                )
                return False
            self._data = {
                "ticker":     ticker,
                "direction":  direction,
                "position_id": position_id,
                "entry_price": entry_price,
                "interval":   interval,
                "opened_at":  time.time(),
            }
            logger.info(
                "[PositionLock] Lock adquirido: %s %s positionId=%s entry=%.4f",
                ticker, direction, position_id, entry_price,
            )
            return True

    def release(self) -> None:
        with self._mu:
            if self._data:
                logger.info(
                    "[PositionLock] Lock liberado: %s %s",
                    self._data.get("ticker"), self._data.get("direction"),
                )
            self._data = {}

    def update_position_id(self, position_id: str) -> None:
        """Actualiza el positionId si eToro devuelve un ID diferente al esperado."""
        with self._mu:
            if self._data:
                self._data["position_id"] = position_id

    # ── Consultas ─────────────────────────────────────────────────────────────

    def get_position_id(self) -> str | None:
        with self._mu:
            return self._data.get("position_id")

    def status(self) -> dict:
        """Dict serializable con el estado actual del lock."""
        with self._mu:
            if not self._data:
                return {"occupied": False}
            d = dict(self._data)
            d["occupied"]     = True
            d["open_seconds"] = int(time.time() - d.get("opened_at", time.time()))
            return d
