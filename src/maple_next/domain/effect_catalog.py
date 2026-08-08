"""Curated, offline effect suggestions for Battle Record v5.

This is deliberately not a battle simulator.  Entries describe only the
deterministic, operator-useful part of a move or ability.  A catalog hit is a
draft suggestion and never mutates canonical state without a human Apply.

Reference data: Pokemon Showdown (MIT), pinned at
``6a1836dd71c0718e923206f3d089e61074410868``.
"""

# ruff: noqa: E501 -- compact, auditable one-entry-per-line reference table

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

SHOWDOWN_SOURCE_COMMIT = "6a1836dd71c0718e923206f3d089e61074410868"
SHOWDOWN_LICENSE = "MIT"
SHOWDOWN_REPOSITORY = "https://github.com/smogon/pokemon-showdown"


class EffectSourceType(StrEnum):
    MOVE = "move"
    ABILITY = "ability"


class EffectTiming(StrEnum):
    SWITCH_IN = "switch_in"
    TURN_START = "turn_start"
    AFTER_ACTION = "after_action"
    OTHER = "other"


class EffectTarget(StrEnum):
    SELF = "self"
    OPPONENT = "opponent"
    BATTLEFIELD = "battlefield"


@dataclass(frozen=True, slots=True)
class EffectCatalogEntry:
    id: str
    display_name_ja: str
    source_type: EffectSourceType
    timing: EffectTiming
    target: EffectTarget
    deterministic_effects: tuple[str, ...]
    conditions_notes: str
    source_reference: str
    source_commit: str = SHOWDOWN_SOURCE_COMMIT

    def __post_init__(self) -> None:
        if not self.id or not self.display_name_ja or not self.deterministic_effects:
            raise ValueError("effect catalog entries require stable identity and effects")
        if self.source_commit != SHOWDOWN_SOURCE_COMMIT:
            raise ValueError("effect catalog entry is not bound to the pinned source commit")

    @property
    def summary(self) -> str:
        return " / ".join(self.deterministic_effects)


def _move(
    id_: str,
    ja: str,
    *effects: str,
    target: EffectTarget = EffectTarget.SELF,
    notes: str = "成功時。失敗・無効化・特殊な反転条件は手動修正。",
) -> EffectCatalogEntry:
    return EffectCatalogEntry(
        id=id_,
        display_name_ja=ja,
        source_type=EffectSourceType.MOVE,
        timing=EffectTiming.AFTER_ACTION,
        target=target,
        deterministic_effects=effects,
        conditions_notes=notes,
        source_reference=f"data/moves.ts#{id_}",
    )


def _ability(
    id_: str,
    ja: str,
    *effects: str,
    target: EffectTarget,
    timing: EffectTiming = EffectTiming.SWITCH_IN,
    notes: str = "発動条件を満たした場合。無効化・上書きは手動修正。",
) -> EffectCatalogEntry:
    return EffectCatalogEntry(
        id=id_,
        display_name_ja=ja,
        source_type=EffectSourceType.ABILITY,
        timing=timing,
        target=target,
        deterministic_effects=effects,
        conditions_notes=notes,
        source_reference=f"data/abilities.ts#{id_}",
    )


# High-value deterministic input aids. Random secondary effects and damage
# amounts are intentionally absent.
EFFECT_CATALOG: tuple[EffectCatalogEntry, ...] = (
    _move("shellsmash", "からをやぶる", "攻撃+2", "防御-1", "特攻+2", "特防-1", "素早さ+2"),
    _move("dragondance", "りゅうのまい", "攻撃+1", "素早さ+1"),
    _move("swordsdance", "つるぎのまい", "攻撃+2"),
    _move("calmmind", "めいそう", "特攻+1", "特防+1"),
    _move("quiverdance", "ちょうのまい", "特攻+1", "特防+1", "素早さ+1"),
    _move("nastyplot", "わるだくみ", "特攻+2"),
    _move("agility", "こうそくいどう", "素早さ+2"),
    _move("rockpolish", "ロックカット", "素早さ+2"),
    _move("irondefense", "てっぺき", "防御+2"),
    _move("amnesia", "ドわすれ", "特防+2"),
    _move("bulkup", "ビルドアップ", "攻撃+1", "防御+1"),
    _move("coil", "とぐろをまく", "攻撃+1", "防御+1", "命中+1"),
    _move("workup", "ふるいたてる", "攻撃+1", "特攻+1"),
    _move(
        "growth", "せいちょう", "攻撃+1", "特攻+1", notes="通常時。晴れ時の増加量は候補を手動修正。"
    ),
    _move("honeclaws", "つめとぎ", "攻撃+1", "命中+1"),
    _move("cosmicpower", "コスモパワー", "防御+1", "特防+1"),
    _move("defendorder", "ぼうぎょしれい", "防御+1", "特防+1"),
    _move("stockpile", "たくわえる", "防御+1", "特防+1", notes="3回まで。蓄積数は手動確認。"),
    _move("shiftgear", "ギアチェンジ", "攻撃+1", "素早さ+2"),
    _move(
        "geomancy", "ジオコントロール", "特攻+2", "特防+2", "素早さ+2", notes="技が完了した場合。"
    ),
    _move("victorydance", "しょうりのまい", "攻撃+1", "防御+1", "素早さ+1"),
    _move(
        "tidyup",
        "おかたづけ",
        "攻撃+1",
        "素早さ+1",
        "設置物とみがわり除去",
        target=EffectTarget.BATTLEFIELD,
    ),
    _move(
        "filletaway",
        "みをけずる",
        "攻撃+2",
        "特攻+2",
        "素早さ+2",
        notes="HP消費と成功を人間が確認。HP量は推測しない。",
    ),
    _move(
        "clangoroussoul",
        "ソウルビート",
        "全能力+1",
        notes="HP消費と成功を人間が確認。HP量は推測しない。",
    ),
    _move("bellydrum", "はらだいこ", "攻撃を最大(+6)", notes="成功とHP消費を人間が確認。"),
    _move("tailglow", "ほたるび", "特攻+3"),
    _move("cottonspore", "わたほうし", "素早さ-2", target=EffectTarget.OPPONENT),
    _move("charm", "あまえる", "攻撃-2", target=EffectTarget.OPPONENT),
    _move("featherdance", "フェザーダンス", "攻撃-2", target=EffectTarget.OPPONENT),
    _move("screech", "いやなおと", "防御-2", target=EffectTarget.OPPONENT),
    _move("faketears", "うそなき", "特防-2", target=EffectTarget.OPPONENT),
    _move("metalsound", "きんぞくおん", "特防-2", target=EffectTarget.OPPONENT),
    _move("scaryface", "こわいかお", "素早さ-2", target=EffectTarget.OPPONENT),
    _move("nobleroar", "おたけび", "攻撃-1", "特攻-1", target=EffectTarget.OPPONENT),
    _move(
        "partingshot",
        "すてゼリフ",
        "攻撃-1",
        "特攻-1",
        target=EffectTarget.OPPONENT,
        notes="低下成功後の交代はactual SWITCHとして記録。",
    ),
    _move("haze", "くろいきり", "全員の能力変化を0", target=EffectTarget.BATTLEFIELD),
    _move(
        "clearsmog",
        "クリアスモッグ",
        "対象の能力変化を0",
        target=EffectTarget.OPPONENT,
        notes="命中・成功時。ダメージ量は推測しない。",
    ),
    _move("topsyturvy", "ひっくりかえす", "対象の能力変化を反転", target=EffectTarget.OPPONENT),
    _move("willowisp", "おにび", "やけど", target=EffectTarget.OPPONENT),
    _move("thunderwave", "でんじは", "まひ", target=EffectTarget.OPPONENT),
    _move("toxic", "どくどく", "もうどく", target=EffectTarget.OPPONENT),
    _move("spore", "キノコのほうし", "ねむり", target=EffectTarget.OPPONENT),
    _move(
        "yawn",
        "あくび",
        "次ターン終了時ねむり候補",
        target=EffectTarget.OPPONENT,
        notes="交代・無効化を含め、実際の発生を後で確認。",
    ),
    _move("raindance", "あまごい", "天候:雨", target=EffectTarget.BATTLEFIELD),
    _move("sunnyday", "にほんばれ", "天候:晴れ", target=EffectTarget.BATTLEFIELD),
    _move("sandstorm", "すなあらし", "天候:砂嵐", target=EffectTarget.BATTLEFIELD),
    _move("snowscape", "ゆきげしき", "天候:雪", target=EffectTarget.BATTLEFIELD),
    _move(
        "electricterrain",
        "エレキフィールド",
        "場:エレキフィールド",
        target=EffectTarget.BATTLEFIELD,
    ),
    _move(
        "grassyterrain", "グラスフィールド", "場:グラスフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _move(
        "mistyterrain", "ミストフィールド", "場:ミストフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _move(
        "psychicterrain", "サイコフィールド", "場:サイコフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _move("trickroom", "トリックルーム", "トリックルーム反転", target=EffectTarget.BATTLEFIELD),
    _move("reflect", "リフレクター", "自分側:リフレクター"),
    _move("lightscreen", "ひかりのかべ", "自分側:ひかりのかべ"),
    _move("auroraveil", "オーロラベール", "自分側:オーロラベール", notes="雪下で成功した場合。"),
    _move("stealthrock", "ステルスロック", "相手側:ステルスロック", target=EffectTarget.OPPONENT),
    _move(
        "spikes",
        "まきびし",
        "相手側:まきびし+1",
        target=EffectTarget.OPPONENT,
        notes="最大層数は現在状態と人間確認。",
    ),
    _move(
        "toxicspikes",
        "どくびし",
        "相手側:どくびし+1",
        target=EffectTarget.OPPONENT,
        notes="最大層数と除去を人間確認。",
    ),
    _move("stickyweb", "ねばねばネット", "相手側:ねばねばネット", target=EffectTarget.OPPONENT),
    _ability("intimidate", "いかく", "攻撃-1", target=EffectTarget.OPPONENT),
    _ability("drizzle", "あめふらし", "天候:雨", target=EffectTarget.BATTLEFIELD),
    _ability("drought", "ひでり", "天候:晴れ", target=EffectTarget.BATTLEFIELD),
    _ability("sandstream", "すなおこし", "天候:砂嵐", target=EffectTarget.BATTLEFIELD),
    _ability("snowwarning", "ゆきふらし", "天候:雪", target=EffectTarget.BATTLEFIELD),
    _ability(
        "electricsurge", "エレキメイカー", "場:エレキフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _ability(
        "grassysurge", "グラスメイカー", "場:グラスフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _ability(
        "mistysurge", "ミストメイカー", "場:ミストフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _ability(
        "psychicsurge", "サイコメイカー", "場:サイコフィールド", target=EffectTarget.BATTLEFIELD
    ),
    _ability(
        "download",
        "ダウンロード",
        "攻撃または特攻+1",
        target=EffectTarget.SELF,
        notes="相手の防御比較で決まった実際の上昇側を人間確認。",
    ),
    _ability("dauntlessshield", "ふくつのたて", "防御+1", target=EffectTarget.SELF),
    _ability("intrepidsword", "ふとうのけん", "攻撃+1", target=EffectTarget.SELF),
)

EFFECT_CATALOG_BY_ID = {entry.id: entry for entry in EFFECT_CATALOG}
EFFECT_CATALOG_BY_JA_NAME = {entry.display_name_ja: entry for entry in EFFECT_CATALOG}


def find_effect(name_or_id: str) -> EffectCatalogEntry | None:
    """Return an offline suggestion by stable id or Japanese display name."""

    normalized = name_or_id.strip().lower().replace(" ", "")
    if normalized in EFFECT_CATALOG_BY_ID:
        return EFFECT_CATALOG_BY_ID[normalized]
    return EFFECT_CATALOG_BY_JA_NAME.get(name_or_id.strip())
