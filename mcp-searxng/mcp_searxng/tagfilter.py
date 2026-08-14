"""Pixiv 标签性别过滤：只保留女性相关作品，剔除男性/男の娘/双性/BL 类。

- 黑名单（BLOCK）：命中任一标签即剔除，覆盖中/日/英变体
- 白名单（FEMALE）：女性相关标签；默认不强制，strict=True 时必须命中其一
- 匹配规则：中/日文子串匹配，英文词边界匹配（\\b），全部大小写不敏感
- tags 兼容两种格式：字符串数组（排行 API）与 {"tag": ...} dict 列表（搜索/详情 API）
"""

import re


class PixivGenderFilter:
    BLOCK = {
        "男性", "男人", "男子", "少年", "男孩", "男の子", "美少年", "男体",
        "おじさん", "おじさま", "おっさん", "じじい", "老人", "爺", "イケメン",
        "青年", "お兄さん", "兄貴", "兄", "弟", "息子", "お父さん", "パパ",
        "オヤジ", "ショタ", "ショタコン", "筋肉", "マッスル", "マッチョ", "ムキムキ",
        "man", "male", "boy", "men", "guy", "guys", "father", "dad",
        "brother", "son", "uncle", "grandpa", "shota", "ikemen", "bishounen",
        "macho", "muscle", "muscular", "bodybuilder",
        "男の娘", "男娘", "伪娘", "女装", "女装男子", "オカマ", "ニューハーフ",
        "おかま", "crossdressing", "cross-dressing", "trap", "traps",
        "femboy", "otokonoko",
        "ふたなり", "二形", "新二形", "扶她", "扶她娘", "futanari", "futa",
        "BL", "耽美", "腐", "男同", "ボーイズラブ", "やおい", "ヤオイ",
        "オメガバース", "総受け", "総攻め", "攻め", "受け", "リバ", "ABO",
        "yaoi", "boyslove", "boys love", "gay", "bl", "bara", "omegaverse",
        "abo", "shounen-ai", "shonen-ai",
    }

    FEMALE = {
        "女性", "女の子", "少女", "美少女", "女子", "女孩", "女生", "妹子",
        "姐姐", "妹妹", "人妻", "妈妈", "妻子", "公主", "女王", "女体",
        "お姉さん", "お姉ちゃん", "お姫様", "姫", "母", "妹", "姉", "妻",
        "ギャル", "レディ", "女神", "女王様",
        "JC", "JK", "JS", "JD",
        "female", "woman", "girl", "girls", "lady", "milf", "waifu",
        "sister", "mother", "princess", "queen",
    }

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict
        self._block = [re.compile(p, re.IGNORECASE) for p in self._patterns(self.BLOCK)]
        self._female = [re.compile(p, re.IGNORECASE) for p in self._patterns(self.FEMALE)]

    @staticmethod
    def _patterns(terms: set[str]):
        for term in sorted(terms):
            if re.fullmatch(r"[A-Za-z]+", term):
                yield rf"\b{term}\b"
            else:
                yield re.escape(term)

    @staticmethod
    def tag_names(tags) -> list[str]:
        names: list[str] = []
        for t in tags or []:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict):
                n = t.get("tag") or t.get("name")
                if n:
                    names.append(str(n))
        return names

    def keep(self, work: dict) -> bool:
        """作品是否保留：未命中黑名单且（strict 时）命中白名单。"""
        tags = self.tag_names(work.get("tags"))
        if not tags:
            return not self.strict
        if any(p.search(t) for p in self._block for t in tags):
            return False
        if self.strict and not any(p.search(t) for p in self._female for t in tags):
            return False
        return True
