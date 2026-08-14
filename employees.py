"""Sales team roster: each seller's role and daily/period sales plan.

This is business data set by the store manager (not something UMAG exposes
via API) -- edit this list whenever the team or the plans change. `name`
just needs to be recognizable; matching against UMAG's "Продавцы" list is
fuzzy (handles reversed word order and small typos).
"""

EMPLOYEES = [
    {"name": "Халметова Наргиза", "position": "РОП", "plan": 11_020_000},
    {"name": "Толегенова Дилнора", "position": "Ст-прод", "plan": 1_347_000},
    {"name": "Яхьяева Зиеда", "position": "Ст-прод", "plan": 1_347_000},
    {"name": "Турдалиева Шахноза", "position": "Ст-прод", "plan": 1_924_000},
    {"name": "Болусов Сардор", "position": "Продавец", "plan": 1_731_000},
    {"name": "Абдимажитов Рустам", "position": "Т-Продавец", "plan": 2_170_000},
    {"name": "Худайбергенова Лайло", "position": "Продавец", "plan": 962_000},
    {"name": "Годеридзе Эмина", "position": "Продавец", "plan": 1_154_000},
    {"name": "Эшметов Дилшат", "position": "Продавец", "plan": 1_154_000},
    {"name": "Файзуллаева Ару", "position": "Продавец", "plan": 770_000},
    {"name": "Болусова Чарос", "position": "Продавец", "plan": 770_000},
    {"name": "Ишанкулов Санжар", "position": "Продавец", "plan": 577_000},
    {"name": "Базарбай Асел", "position": "Продавец", "plan": 577_000},
]
