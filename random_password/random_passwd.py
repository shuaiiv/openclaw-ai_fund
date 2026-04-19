#!/usr/bin/env python3
"""
随机密码生成器
用法: python random_passwd.py [长度] [大写] [小写] [数字] [符号]
"""

import random
import string
import sys


DEFAULT_SYMBOLS = "!@#$%^&*()-_=+[]{}|;:,.<>?"


def parse_args(args):
    length = 20
    use_upper = 1
    use_lower = 1
    use_digits = 1
    symbols_arg = "1"

    if len(args) >= 1:
        try:
            length = int(args[0])
            if not (6 <= length <= 60):
                print(f"错误：密码长度必须在 6-60 之间，当前值: {length}")
                sys.exit(1)
        except ValueError:
            print(f"错误：第一个参数（长度）必须是整数，当前值: {args[0]}")
            sys.exit(1)

    if len(args) >= 2:
        if args[1] not in ("0", "1"):
            print(f"错误：第二个参数（大写字母）必须是 0 或 1，当前值: {args[1]}")
            sys.exit(1)
        use_upper = int(args[1])

    if len(args) >= 3:
        if args[2] not in ("0", "1"):
            print(f"错误：第三个参数（小写字母）必须是 0 或 1，当前值: {args[2]}")
            sys.exit(1)
        use_lower = int(args[2])

    if len(args) >= 4:
        if args[3] not in ("0", "1"):
            print(f"错误：第四个参数（数字）必须是 0 或 1，当前值: {args[3]}")
            sys.exit(1)
        use_digits = int(args[3])

    if len(args) >= 5:
        symbols_arg = args[4]

    return length, use_upper, use_lower, use_digits, symbols_arg


def resolve_symbols(symbols_arg):
    """解析符号参数，返回 (use_symbols: bool, symbol_pool: str)"""
    if symbols_arg == "0":
        return False, ""
    elif symbols_arg == "1":
        return True, DEFAULT_SYMBOLS
    else:
        # 当作符号列表处理
        cleaned = symbols_arg.strip()
        if not cleaned:
            print("错误：提供的符号列表为空")
            sys.exit(1)
        return True, cleaned


def calc_min_counts(length, use_upper, use_lower, use_digits, use_symbols):
    """
    根据规则计算各类字符的最小数量。
    规则：
    - 数字和符号都启用：各自 >= length // 4
    - 数字/符号只有一个启用：该类 >= length // 3
    - 大写或小写有一个启用：字母合计 >= length // 2
      其中大写和小写都启用时，各自至少占字母下限的 1/3（即总长度的 1/6）
    """
    min_digits = 0
    min_symbols = 0
    min_letters = 0  # 字母总下限

    both_ds = use_digits and use_symbols
    if both_ds:
        min_digits = length // 4
        min_symbols = length // 4
    else:
        if use_digits:
            min_digits = length // 3
        if use_symbols:
            min_symbols = length // 3

    any_letters = use_upper or use_lower
    if any_letters:
        min_letters = length // 2

    return min_digits, min_symbols, min_letters


def generate_password(length, use_upper, use_lower, use_digits, use_symbols, symbol_pool):
    upper_pool  = string.ascii_uppercase if use_upper  else ""
    lower_pool  = string.ascii_lowercase if use_lower  else ""
    digit_pool  = string.digits          if use_digits else ""
    sym_pool    = symbol_pool            if use_symbols else ""

    all_pool = upper_pool + lower_pool + digit_pool + sym_pool
    if not all_pool:
        print("错误：所有字符类型均被禁用，无法生成密码")
        sys.exit(1)

    min_digits, min_symbols, min_letters = calc_min_counts(
        length, use_upper, use_lower, use_digits, use_symbols
    )

    # 构建必须包含的字符
    mandatory = []

    # 数字
    if use_digits:
        mandatory += random.choices(digit_pool, k=min_digits)

    # 符号
    if use_symbols:
        mandatory += random.choices(sym_pool, k=min_symbols)

    # 字母
    if min_letters > 0:
        if use_upper and use_lower:
            # 两者都启用：各自至少占 min_letters 的 1/3，剩余从混合池随机填充
            each_min = min_letters // 3
            mandatory += random.choices(upper_pool, k=each_min)
            mandatory += random.choices(lower_pool, k=each_min)
            rest = min_letters - each_min * 2
            mandatory += random.choices(upper_pool + lower_pool, k=rest)
        elif use_upper:
            mandatory += random.choices(upper_pool, k=min_letters)
        else:
            mandatory += random.choices(lower_pool, k=min_letters)
    else:
        # min_letters == 0（理论上不会触发，保留兜底）
        if use_upper:
            mandatory += random.choices(upper_pool, k=1)
        if use_lower:
            mandatory += random.choices(lower_pool, k=1)
    if use_digits and min_digits == 0:
        mandatory += random.choices(digit_pool, k=1)
    if use_symbols and min_symbols == 0:
        mandatory += random.choices(sym_pool, k=1)

    if len(mandatory) > length:
        print(
            f"错误：必须包含的最少字符数（{len(mandatory)}）超过了密码长度（{length}），"
            "请增加密码长度或放宽字符类型限制"
        )
        sys.exit(1)

    # 用全字符池填满剩余位置
    remaining = length - len(mandatory)
    password_chars = mandatory + random.choices(all_pool, k=remaining)

    # 打乱顺序
    random.shuffle(password_chars)
    return "".join(password_chars)


def main():
    args = sys.argv[1:]
    length, use_upper, use_lower, use_digits, symbols_arg = parse_args(args)
    use_symbols, symbol_pool = resolve_symbols(symbols_arg)

    if not any([use_upper, use_lower, use_digits, use_symbols]):
        print("错误：至少需要启用一种字符类型（大写/小写/数字/符号）")
        sys.exit(1)

    password = generate_password(length, use_upper, use_lower, use_digits, use_symbols, symbol_pool)

    # 打印摘要
    flags = []
    if use_upper:  flags.append("大写字母")
    if use_lower:  flags.append("小写字母")
    if use_digits: flags.append("数字")
    if use_symbols:
        if symbols_arg not in ("0", "1"):
            flags.append(f"符号({symbol_pool})")
        else:
            flags.append("符号")

    print(f"密码长度: {length}，包含: {', '.join(flags)}")
    print(f"生成的密码: {password}")


if __name__ == "__main__":
    main()
