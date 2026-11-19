"""Curva ABC: classificação dos clientes pelo valor pago no mês de referência.

As faixas NÃO ficam no código. Elas são lidas da tabela `curvas_abc` e podem ser editadas na
tela de configurações (ver `repository.load_faixas` / `repository.save_faixas`).

Regras:
  * Valores em centavos (inteiros). Cada faixa vai de `minimo_centavos` até `maximo_centavos`, inclusive.
  * A+ não tem máximo. As faixas precisam cobrir todos os valores a partir de zero, sem sobreposição
    e sem lacunas: o máximo de cada faixa é exatamente 1 centavo abaixo do mínimo da faixa acima.
  * Valor não informado (None) NÃO é classificado.
"""
from __future__ import annotations

from .validation import format_money

# Ordem da maior para a menor faixa. Os nomes são fixos; os limites são configuráveis.
LABELS = ("A+", "A", "B", "C")

# Valores iniciais: usados apenas para criar a tabela pela primeira vez (ver db.py).
# A partir daí, o sistema sempre usa o que estiver salvo no banco.
VALORES_INICIAIS = (
    ("A+", 100_000, None),    # R$ 1.000,00 ou mais
    ("A", 40_000, 99_999),    # R$ 400,00 até R$ 999,99
    ("B", 18_000, 39_999),    # R$ 180,00 até R$ 399,99
    ("C", 0, 17_999),         # R$ 0,00 até R$ 179,99
)


def classify(amount_cents: int | None, faixas: list[dict]) -> str | None:
    """Retorna a classificação do valor segundo as faixas dadas (ordenadas da maior para a menor)."""
    if amount_cents is None:
        return None
    for faixa in faixas:
        minimo, maximo = faixa["minimo_centavos"], faixa["maximo_centavos"]
        if amount_cents >= minimo and (maximo is None or amount_cents <= maximo):
            return faixa["classificacao"]
    return None   # não ocorre com faixas validadas (a faixa C começa em zero)


def ordenar(faixas: list[dict]) -> list[dict]:
    """Ordena as faixas da maior para a menor classificação."""
    rank = {label: i for i, label in enumerate(LABELS)}
    return sorted(faixas, key=lambda f: rank.get(f["classificacao"], 99))


def validate_faixas(faixas: list[dict]) -> dict[str, str]:
    """Valida as faixas. Retorna {campo: mensagem}; vazio quando estão corretas.

    Campos: "geral", "<classe>.minimo" e "<classe>.maximo".
    Cada faixa é um dict com classificacao, minimo_centavos (int ou None) e maximo_centavos (int ou None).
    """
    errors: dict[str, str] = {}
    by = {}
    for f in faixas:
        by.setdefault(f["classificacao"], f)
    if sorted(by) != sorted(LABELS) or len(faixas) != len(LABELS):
        return {"geral": "Informe as quatro classificações (A+, A, B e C), uma vez cada."}

    for label in LABELS:
        f = by[label]
        minimo, maximo = f["minimo_centavos"], f["maximo_centavos"]
        if minimo is None:
            errors[f"{label}.minimo"] = "Informe o valor mínimo."
        elif minimo < 0:
            errors[f"{label}.minimo"] = "O valor mínimo não pode ser negativo."
        if label == "A+":
            if maximo is not None:
                errors["A+.maximo"] = "A+ não tem limite máximo: deixe este campo em branco."
        elif maximo is None:
            errors[f"{label}.maximo"] = "Informe o valor máximo."
        elif minimo is not None and maximo < minimo:
            errors[f"{label}.maximo"] = "O máximo deve ser igual ou maior que o mínimo."
    if errors:
        return errors

    if by["C"]["minimo_centavos"] != 0:
        errors["C.minimo"] = "A faixa C deve começar em R$ 0,00, para que todo valor tenha classificação."
    ordered = [by[label] for label in LABELS]            # A+, A, B, C
    for upper, lower in zip(ordered, ordered[1:]):
        lower_max, upper_min = lower["maximo_centavos"], upper["minimo_centavos"]
        field = f"{lower['classificacao']}.maximo"
        if lower_max >= upper_min:
            errors[field] = (f"Sobrepõe a faixa {upper['classificacao']}. Ajuste o máximo de "
                             f"{lower['classificacao']} (até {format_money(upper_min - 1)}) ou o mínimo de "
                             f"{upper['classificacao']}.")
        elif lower_max + 1 < upper_min:
            errors[field] = (f"Há valores sem classificação entre {format_money(lower_max + 1)} e "
                             f"{format_money(upper_min - 1)}. Ajuste o máximo de {lower['classificacao']} "
                             f"ou o mínimo de {upper['classificacao']} para não deixar lacunas.")
    return errors


def describe_faixas(faixas: list[dict]) -> list[dict]:
    """Faixas no formato usado pela legenda da interface."""
    return [{"classificacao": f["classificacao"], "min_centavos": f["minimo_centavos"],
             "max_centavos": f["maximo_centavos"]} for f in ordenar(faixas)]
