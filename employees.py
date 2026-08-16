"""Sales team roster: each seller's role and daily/period sales plan.

This is business data set by the store manager (not something UMAG exposes
via API) -- edit this list whenever the team or the plans change. `name`
just needs to be recognizable; matching against UMAG's "Продавцы" list is
fuzzy (handles reversed word order and small typos).

`store` selects which UMAG retail point (see UmagClient.select_store) the
employee's sales are pulled from -- defaults to "Iposuda" when omitted.
This account has multiple retail points ("Iposuda", "Kids", "Dubai Gold");
Ikids (2 этаж) sells under the "Kids" point in UMAG.
"""

EMPLOYEES = [
    # Халметова Наргиза (РОП) намеренно исключена: в UMAG на неё была
    # подвязана агрегированная выручка магазина, а не личные продажи --
    # теперь это отдельная метрика "Общая выручка" в KPI-дашборде
    # (Settings.totalRevenuePlan), не привязанная к конкретному сотруднику.
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
    # Колл-центр. Управляющий дал план как месячный (32 787 500 и 10 000 000) --
    # здесь он пересчитан на дневной (/26 рабочих дней) для дневного отчёта
    # /report; в KPI-дашборде у них хранится настоящий месячный план напрямую.
    {"name": "Юлдашбекова Мухлиса", "position": "Оператор", "plan": 1_261_096},
    {"name": "Артыкова Лолахон", "position": "Оператор", "plan": 384_615},
    # Ikids (2 этаж) -- отдельная торговая точка в UMAG ("Kids"). Планы
    # даны как месячные (10 000 000 каждому), здесь пересчитаны на дневной
    # (/26 рабочих дней) для дневного отчёта /report, как и у остальных.
    {"name": "Илашбеков Ахрар", "position": "Продавец", "plan": 384_615, "store": "Kids"},
    {"name": "Султанмурадова Шахноза", "position": "Продавец", "plan": 384_615, "store": "Kids"},
    {"name": "Бахтиярова Дияра", "position": "Продавец", "plan": 384_615, "store": "Kids"},
    {"name": "Нишанбаева Анеля", "position": "Продавец", "plan": 384_615, "store": "Kids"},
]
