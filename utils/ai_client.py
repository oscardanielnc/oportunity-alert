"""
AIClient — motor de IA centralizado para todo el sistema.

Un único punto de entrada para llamadas a Gemini y Claude.
Todos los módulos del sistema importan de aquí:
  - filters/claude_scorer.py
  - utils/premarket_scanner.py
  - cualquier estrategia futura

Motor seleccionado por AI_ENGINE env var: "gemini" (default) | "claude"
Modelos por defecto sobreridables con GEMINI_MODEL / CLAUDE_MODEL.
"""
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Modelos por defecto
_GEMINI_DEFAULT = "gemini-2.0-flash"
_CLAUDE_DEFAULT = "claude-haiku-4-5-20251001"


def call_ai(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 512,
    engine: str = None,
    model: str = None,
    temperature: float = 0.1,
    force_json: bool = True,
) -> Optional[str]:
    """
    Llama a Gemini o Claude y retorna el texto raw de la respuesta.
    Retorna None en cualquier error — el caller maneja su fallback.

    Args:
        prompt:      Prompt de usuario (requerido)
        system:      Instrucción de sistema / system prompt (opcional)
        max_tokens:  Máximo de tokens en la respuesta
        engine:      "gemini" | "claude" — por defecto usa AI_ENGINE env var
        model:       Modelo explícito (Claude). Si se pasa, MANDA sobre la env var CLAUDE_MODEL
                     (config.json = fuente de verdad; env var = override opcional). None → env/default.
        temperature: 0.1 = determinístico, 1.0 = creativo
        force_json:  Gemini: añade response_mime_type="application/json"
    """
    eng = (engine or os.environ.get("AI_ENGINE", "gemini")).lower()

    try:
        if eng == "gemini":
            return _call_gemini(
                prompt, system=system, max_tokens=max_tokens,
                temperature=temperature, force_json=force_json,
            )
        if eng == "claude":
            return _call_claude(
                prompt, system=system, max_tokens=max_tokens,
                temperature=temperature, model=model,
            )
        logger.error(f"[AIClient] Motor desconocido: {eng!r} — usar 'gemini' o 'claude'")
        return None
    except Exception as e:
        logger.error(f"[AIClient] Error inesperado ({eng}): {e}")
        return None


def parse_json_response(text: Optional[str]) -> Optional[dict]:
    """
    Extrae y parsea el primer objeto JSON de una respuesta de IA.
    Intenta parse directo primero; si falla, busca el primer bloque {...}.
    Retorna None si no hay texto o no hay JSON válido.
    """
    if not text:
        return None

    # Intento directo (respuesta limpia)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extraer primer bloque JSON (cuando la IA añade texto antes/después)
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    logger.warning(f"[AIClient] No se encontró JSON válido en respuesta: {text[:120]!r}")
    return None


# ── Implementaciones por motor ────────────────────────────────────────────────

def _call_gemini(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 512,
    temperature: float = 0.1,
    force_json: bool = True,
) -> Optional[str]:
    try:
        import google.generativeai as genai
    except ImportError:
        logger.error("[AIClient] google-generativeai no instalado: pip install google-generativeai")
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("[AIClient] GEMINI_API_KEY no configurada")
        return None

    model_name = os.environ.get("GEMINI_MODEL", _GEMINI_DEFAULT)
    gen_config = {"temperature": temperature, "max_output_tokens": max_tokens}
    if force_json:
        gen_config["response_mime_type"] = "application/json"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system if system else None,
        generation_config=gen_config,
    )

    response = model.generate_content(prompt)
    text = response.text.strip()

    # Quitar fences de markdown si Gemini los añade (```json ... ```)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()

    logger.debug(f"[AIClient] Gemini {model_name} OK ({len(text)} chars)")
    return text


def _call_claude(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 512,
    temperature: float = 0.1,
    model: str = None,
) -> Optional[str]:
    try:
        import anthropic
    except ImportError:
        logger.error("[AIClient] anthropic no instalado: pip install anthropic")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("[AIClient] ANTHROPIC_API_KEY no configurada")
        return None

    # Precedencia: model explícito (config.json vía main) > env CLAUDE_MODEL > default Haiku.
    # Antes el `model` no llegaba hasta acá (código muerto) y el modelo real lo decidía SOLO la
    # env var → si CLAUDE_MODEL se perdía, caía a Haiku en silencio. Ahora config.json manda.
    model_name = model or os.environ.get("CLAUDE_MODEL", _CLAUDE_DEFAULT)
    client = anthropic.Anthropic(api_key=api_key)

    kwargs: dict = {
        "model":     model_name,
        "max_tokens": max_tokens,
        "messages":  [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    message = client.messages.create(**kwargs)
    text = message.content[0].text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()

    logger.debug(f"[AIClient] Claude {model_name} OK ({len(text)} chars)")
    return text
