import sys
from pathlib import Path
import os
import argparse

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.ai.nlp_query import ask


def main():
    parser = argparse.ArgumentParser(description="自然言語で家計データに問い合わせます。")
    parser.add_argument(
        'question',
        nargs='?',
        help="質問文 (例: '先月の外食費はいくら？')"
    )
    args = parser.parse_args()

    print("==================================================")
    print("  自然言語クエリ")
    print("==================================================")

    os.chdir(project_root)

    if args.question:
        answer = ask(args.question)
        print(f"\n{answer}")
    else:
        # 対話モード
        print("質問を入力してください (終了: quit)\n")
        while True:
            question = input("Q: ").strip()
            if question.lower() in ('quit', 'exit', 'q'):
                break
            if not question:
                continue
            answer = ask(question)
            print(f"A: {answer}\n")

    print("==================================================")


if __name__ == "__main__":
    main()
