#!/bin/bash
# 添加 adj_factor hourly cron 条目 (B-08) — 幂等, 可重复执行。
# 失败时不改动现有 crontab, 并打印逐行诊断。
CRON_LINE='50 * * * * cd /Users/mariusto/project/quant && bash scripts/run_task.sh adj_factor >> logs/cron.log 2>&1'

TMP=$(mktemp /tmp/cron_new.XXXXXX)
BAK=$(mktemp /tmp/cron_bak.XXXXXX)

# 1. 备份现有 crontab (为空也可以)
crontab -l 2>/dev/null > "$BAK" || true
echo "backup: $BAK"

# 2. 去重后追加
grep -v "run_task.sh adj_factor" "$BAK" > "$TMP" || true
echo "$CRON_LINE" >> "$TMP"

# 3. 预检: 找出明显的非法行 (非注释/非空/非 VAR=x 的行, 必须以 5 个时间字段或 @开头)
bad=0
lineno=0
while IFS= read -r line; do
    lineno=$((lineno + 1))
    case "$line" in
        ''|\#*) continue ;;
        *=*)  continue ;;   # 环境变量赋值
        @*)   continue ;;   # @daily 等关键字
    esac
    # 至少 5 个空白分隔字段, 且第一个字段是合法分钟 (* 数字 , - /)
    nfields=$(echo "$line" | awk '{print NF}')
    first=$(echo "$line" | awk '{print $1}')
    if [ "$nfields" -lt 6 ] || ! echo "$first" | grep -qE '^[0-9*,/\-]+$'; then
        echo "SUSPECT line $lineno: $line"
        bad=1
    fi
done < "$TMP"
if [ "$bad" -eq 1 ]; then
    echo "--- 现有 crontab 里有非法行 (见上), 请先手动修复; 未做任何改动 ---"
    echo "--- 完整内容: ---"
    cat -n "$TMP"
    exit 1
fi

# 4. 安装
if crontab "$TMP" 2>&1; then
    echo "OK: installed. 当前 adj_factor 条目:"
    crontab -l | grep adj_factor
    rm -f "$TMP" "$BAK"
else
    echo "ERROR: crontab install failed; 现有 crontab 未被改动"
    echo "--- 尝试安装的内容 (行号): ---"
    cat -n "$TMP"
    exit 1
fi
