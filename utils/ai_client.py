"""
AIClient — motor de IA centralizado para todo el sistema.

Un único punto de entrada para llamadas a DeepSeek, GLM, Gemini y Claude.
Todos los módulos del sistema importan de aquí:
  - filters/claude_scorer.py   (noticias  → tier FUERTE)
  - utils/daily_brief.py       (brief     → tier FUERTE — estrategia macro, 2026-06-23)
  - utils/trump_tracker.py     (radar     → tier BARATO)
  - cualquier estrategia futura

Motor seleccionado por AI_ENGINE env var: "deepseek" (default) | "glm" | "gemini" | "claude".

DOS TIERS (Oscar, 2026-06-19 — migración a DeepSeek):
  - FUERTE: decisiones financieras (scoring de noticias). Default deepseek-v4-pro.
  - BARATO: redacción/resumen (brief, radar Trump). Default deepseek-v4-flash.
Usar resolve_engine_model(tier) para elegir; override fino con AI_MODEL_STRONG /
AI_MODEL_CHEAP (o el *_MODEL legacy que ya pasan los módulos).

DeepSeek/GLM son APIs compatibles con OpenAI → se hablan por REST con `requests`
(cero dependencias nuevas). Claves: DEEPSEEK_API_KEY / GLM_API_KEY.
"""
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Modelos por defecto (legacy, motores viejos)
_GEMINI_DEFAULT = "gemini-2.0-flash"
_CLAUDE_DEFAULT = "claude-haiku-4-5-20251001"

# Endpoints OpenAI-compatible (override con *_BASE_URL si el proveedor cambia el host)
_DEEPSEEK_BASE = "https://api.deepseek.com"
_GLM_BASE      = "https://api.z.ai/api/openai/v1"

# Defaults por motor y tier. AI_MODEL_STRONG / AI_MODEL_CHEAP mandan sobre esto.
# DeepSeek (verificado en vivo 2026-06-19):
#   - STRONG = deepseek-v4-pro (razonador): decisiones financieras, devuelve JSON con
#     razonamiento. Necesita max_tokens holgado (el "pensamiento" consume tokens).
#   - CHEAP  = deepseek-chat (NO-pensante): radar Trump = resumir. Más rápido y barato,
#     sin riesgo de que el razonamiento trunque la salida. (deepseek-chat se depreca el
#     2026-07-24 → migrar a su sucesor no-pensante; override con AI_MODEL_CHEAP.)
_TIER_DEFAULTS = {
    "deepseek": {"strong": "deepseek-v4-pro",   "cheap": "deepseek-chat"},
    "glm":      {"strong": "glm-5.1",            "cheap": "glm-4.7-flash"},
    "claude":   {"strong": "claude-sonnet-4-6",  "cheap": "claude-haiku-4-5-20251001"},
    "gemini":   {"strong": "gemini-2.0-flash",   "cheap": "gemini-2.0-flash"},
}


def resolve_engine_model(tier: str = "cheap", *, engine: str = None,
                         model: str = None) -> tuple:
    """
    Resuelve (engine, model) para un tier dado ("strong" | "cheap").

    Precedencia del modelo: `model` explícito > env AI_MODEL_STRONG/AI_MODEL_CHEAP >
    default del motor para ese tier. El motor sale de `engine` o AI_ENGINE (default
    deepseek). Punto único de verdad para que ningún módulo hardcodee a Claude.
    """
    eng = (engine or os.environ.get("AI_ENGINE", "deepseek")).lower()
    if model:
        return eng, model
    env_key = "AI_MODEL_STRONG" if tier == "strong" else "AI_MODEL_CHEAP"
    model = os.environ.get(env_key) or _TIER_DEFAULTS.get(eng, {}).get(tier)
    return eng, model


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
    eng = (engine or os.environ.get("AI_ENGINE", "deepseek")).lower()

    try:
        if eng == "deepseek":
            return _call_openai_compatible(
                prompt, system=system, max_tokens=max_tokens, temperature=temperature,
                force_json=force_json, model=model, label="DeepSeek",
                base_url=os.environ.get("DEEPSEEK_BASE_URL", _DEEPSEEK_BASE),
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                default_model=_TIER_DEFAULTS["deepseek"]["cheap"],
            )
        if eng == "glm":
            return _call_openai_compatible(
                prompt, system=system, max_tokens=max_tokens, temperature=temperature,
                force_json=force_json, model=model, label="GLM",
                base_url=os.environ.get("GLM_BASE_URL", _GLM_BASE),
                api_key=os.environ.get("GLM_API_KEY", "") or os.environ.get("ZHIPU_API_KEY", ""),
                default_model=_TIER_DEFAULTS["glm"]["cheap"],
            )
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
        logger.error(f"[AIClient] Motor desconocido: {eng!r} — usar 'deepseek', 'glm', 'gemini' o 'claude'")
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

def _call_openai_compatible(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 512,
    temperature: float = 0.1,
    force_json: bool = True,
    model: str = None,
    base_url: str = "",
    api_key: str = "",
    default_model: str = "",
    label: str = "OpenAI-compat",
) -> Optional[str]:
    """
    Llama a cualquier API compatible con OpenAI (DeepSeek, GLM/Z.ai) por REST.
    Usa `requests` (sin dependencias nuevas). Retorna el texto de la respuesta o None.

    JSON mode: DeepSeek/GLM aceptan response_format={"type":"json_object"} pero exigen
    que el prompt contenga la palabra "json" — nuestros prompts ya la piden, así que es
    seguro activarlo. Si el modelo no soporta el flag, igual responde (best-effort).
    """
    import requests

    if not api_key:
        logger.error(f"[AIClient] {label}: API key no configurada "
                     f"(¿DEEPSEEK_API_KEY / GLM_API_KEY?)")
        return None

    model_name = model or default_model
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
    except Exception as e:
        logger.error(f"[AIClient] {label} error de red: {e}")
        return None

    if r.status_code != 200:
        logger.error(f"[AIClient] {label} HTTP {r.status_code}: {r.text[:200]}")
        return None

    try:
        data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.error(f"[AIClient] {label} respuesta inesperada: {e} | {r.text[:200]}")
        return None

    # Algunos modelos razonadores envuelven en fences ```json ... ```
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()

    logger.debug(f"[AIClient] {label} {model_name} OK ({len(text)} chars)")
    return text


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
