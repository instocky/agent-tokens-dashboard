"""config.py — pricing per model (USD per 1M tokens).

Цена в USD за 1 миллион токенов. Источник — модельные карточки провайдера
(скриншот MiniMax-M3: $0.23/M input, $0.96/M output).

Использование: `compute_cost(input_tokens, output_tokens, model)` из билдеров
дашбордов. `model` — ключ в MODEL_PRICING (например "MiniMax-M3").
Неизвестная модель → ValueError (явный fail-fast лучше молчаливого $0.00:
если рендер показал $0, юзер подумает что расход бесплатный, а не что мы
забыли таблицу).

DEFAULT_MODEL — текущая модель runtime'а; до добавления настоящего
multi-model сценария (build_dashboard.py читает `local_runtime_sessions`
и прокидывает model_id) — все расчёты идут по этому ключу.
"""
from __future__ import annotations


# USD за 1M токенов. Ключ — model_id (значение runtime_state.model).
# При появлении новой модели добавляем строку сюда + опционально DEFAULT_MODEL.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # MiniMax-M3: $0.23 / 1M input, $0.96 / 1M output (model card 2026-05-31).
    "MiniMax-M3": {"input": 0.23, "output": 0.96},
}
DEFAULT_MODEL: str = "MiniMax-M3"


def compute_cost(
    input_tokens: int, output_tokens: int, model: str = DEFAULT_MODEL
) -> float:
    """Стоимость в USD для заданного объёма input/output.

    Formula:
        cost = (input / 1_000_000) * price_in
             + (output / 1_000_000) * price_out

    Параметры:
      - input_tokens, output_tokens: целые ≥ 0. Отрицательные значения дают
        отрицательный cost (валидный результат арифметически, но не должен
        возникать из SQL — защиты нет, доверяем входу).
      - model: ключ в MODEL_PRICING. По умолчанию DEFAULT_MODEL.

    Raises:
      ValueError: модель не найдена в MODEL_PRICING. Лучше упасть в билдере,
        чем молча отрендерить $0.00 — это даст ложный сигнал «расход = 0».
    """
    if model not in MODEL_PRICING:
        raise ValueError(
            f"Unknown model {model!r}. Known: {sorted(MODEL_PRICING)}"
        )
    p = MODEL_PRICING[model]
    return (input_tokens / 1_000_000.0) * p["input"] + (
        output_tokens / 1_000_000.0
    ) * p["output"]


def fmt_money(amount: float) -> str:
    """Человеко-читаемая цена в USD: 5.4 → '$5.40', 1234.5 → '$1,234.50'.

    Всегда два знака после запятой — единообразно с банковскими/бухгалтерскими
    конвенциями и с тем, как цены отображаются в модельных карточках. Trailing
    '.00' убрать специально (например $0.00 → "$0.00" нормально, это «бесплатно»
    а не «ошибка округления»). Применяется ТОЛЬКО для подписей на дашборде.
    """
    return f"${amount:,.2f}"
