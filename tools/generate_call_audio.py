# -*- coding: utf-8 -*-
"""呼び出しモニター用の音声ファイルを edge-tts で一括生成する。

GoogleTV等のブラウザには日本語TTSエンジンがなく Web Speech API が使えないため、
事前生成した音声ファイルを WebAudio で連結再生する方式を取る。

生成物 (audio/ 以下):
  ready.mp3        「音声の準備ができました」(有効化時の動作確認用)
  prefix.mp3       「整理番号、」
  num/{1..300}.mp3 「{n}番のかた、」
  room1.mp3        「第1診察室へ、おはいりください。」
  room2.mp3        「第2診察室へ、おはいりください。」

再生時は prefix → num/{n} → room{r} の順に連結する。
使い方:  python tools/generate_call_audio.py
"""
import asyncio
from pathlib import Path

import edge_tts

VOICE = "ja-JP-NanamiNeural"
RATE = "-10%"   # 院内アナウンスらしく少しゆっくり
NUM_MAX = 300   # 整理番号の上限。増やす場合はこの値を変えて再実行(既存はスキップされる)

BASE = Path(__file__).resolve().parent.parent / "audio"
CONCURRENCY = 8

# edge-tts は数字の下一桁が「8」の場合、本来の「じゅうはち」ではなく促音化した
# 「じゅっぱち」のように誤読することがあるため、該当する番号だけひらがな読みを
# 明示して正しい発音を強制する。新たに同様の誤読が見つかった場合はここに追加する。
NUM_READING_OVERRIDES = {
    18: "じゅうはち",
    28: "にじゅうはち",
    38: "さんじゅうはち",
    48: "よんじゅうはち",
    58: "ごじゅうはち",
    68: "ろくじゅうはち",
    78: "ななじゅうはち",
    88: "はちじゅうはち",
    98: "きゅうじゅうはち",
}


def reading_for(n: int) -> str:
    return NUM_READING_OVERRIDES.get(n, str(n))


async def gen(text: str, path: Path, sem: asyncio.Semaphore):
    if path.exists():
        return
    async with sem:
        tts = edge_tts.Communicate(text, VOICE, rate=RATE)
        await tts.save(str(path))
        print(f"OK {path.name}: {text}")


async def main():
    (BASE / "num").mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        gen("音声の準備ができました", BASE / "ready.mp3", sem),
        gen("整理番号、", BASE / "prefix.mp3", sem),
        # 「かた」は漢字の「方」だと誤読リスクがあるためひらがな指定
        gen("第1診察室へ、おはいりください。", BASE / "room1.mp3", sem),
        gen("第2診察室へ、おはいりください。", BASE / "room2.mp3", sem),
    ]
    tasks += [gen(f"{reading_for(n)}番のかた、", BASE / "num" / f"{n}.mp3", sem) for n in range(1, NUM_MAX + 1)]
    await asyncio.gather(*tasks)
    print(f"完了: {sum(1 for _ in BASE.rglob('*.mp3'))} ファイル")


if __name__ == "__main__":
    asyncio.run(main())
