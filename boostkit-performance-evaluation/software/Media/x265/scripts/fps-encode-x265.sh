#!/bin/bash

# 检查用户输入的参数
if [ $# -lt 1 ]; then
    echo "Usage: $0 [cpu_binding_type (4core/8core/16core)] [machine_type (920/x86)] [number_of_routes]"
    exit 1
fi

CPU_BINDING_TYPE=${1:-8core}
MACHINE_TYPE=${2:-920}
NUM_ROUTES=${3:-20}

# 定义变量
export LD_LIBRARY_PATH=$TEST_HOME/x265_4.2/x265_install/lib/
export PKG_CONFIG_PATH=$TEST_HOME/x265_4.2/x265_install/pkgconfig/
x265="$TEST_HOME/x265_4.2/x265_install/bin/x265"
VIDEO_DIR="$TEST_HOME/video"
OUTDIR_BASE="$TEST_HOME/outputfiles-x265"
BITRATES=(2000 4000 6000 8000)  # 码率数组

declare -a pid_array  # 存储进程 ID 的数组

# 计算每一路的 CPU 绑定范围
calculate_cpu_range() {
    local i=$1
    local CPU_BINDING_TYPE=$2
    local MACHINE_TYPE=$3
    local CPU_OFFSET=192

    if [ "$CPU_BINDING_TYPE" == "4core" ]; then
        cpu_start=$(( (i - 1) * 4 ))
        cpu_end=$(( cpu_start + 3 ))
    elif [ "$CPU_BINDING_TYPE" == "8core" ]; then
        cpu_start=$(( (i - 1) * 8 ))
        cpu_end=$(( cpu_start + 7 ))
    elif [ "$CPU_BINDING_TYPE" == "16core" ]; then
        cpu_start=$(( (i - 1) * 16 ))
        cpu_end=$(( cpu_start + 15 ))
    else
        echo "Invalid CPU binding type. Use 4core, 8core, or 16core." >&2
        exit 1
    fi

    if [ "$MACHINE_TYPE" == "x86" ]; then
        echo "$cpu_start-$cpu_end,$(($CPU_OFFSET + cpu_start))-$(($CPU_OFFSET + cpu_end))"
    else
        echo "$cpu_start-$cpu_end"
    fi
}

for video in "$VIDEO_DIR"/*.yuv; do
    base_name=$(basename "$video" .yuv)  # 获取视频名
    for bitrate in "${BITRATES[@]}"; do
        outdir="${OUTDIR_BASE}/${bitrate}k/${base_name}"  # 按码率和视频名创建子目录
        mkdir -p "$outdir"  # 创建目录

        # 设置默认参数
        preset="medium"
        fps=24
        frames=9999
        keyint=48
        min_keyint=24
        bframes=3
        rc_lookahead=4
        frame_threads=4
        pools=16
        lookahead_threads=3

        # 根据不同的视频文件设置不同的参数
        case "$base_name" in
            #低时延
            "Dota2_1920x1080_60")
                preset="faster"
                fps=60
                keyint=120
                min_keyint=60
                bframes=0
                rc_lookahead=1
                ;;
            #高清
            "xihuanni_1920x1080_30_1000")
                preset="faster"
                fps=30
                keyint=60
                min_keyint=30
                ;;
            "showSingText_1920x1080_40")
                preset="faster"
                fps=40
                keyint=80
                min_keyint=40
                ;;
            "1_Yuanshen_1920x1080_60_8bit_12M")
                preset="faster"
                fps=60
                keyint=120
                min_keyint=60
                ;;
            "BQTerrace_1920x1080_60")
                preset="faster"
                fps=60
                keyint=120
                min_keyint=60
                ;;
            #点播
            "Film2012_1920x1080_25_8bit_8Mbps")
                preset="slower"
                fps=25
                keyint=50
                min_keyint=25
                bframes=7
                rc_lookahead=25
                ;;
            "BQTerrace_1920x1080_60-dianbo")
                preset="slower"
                fps=60
                keyint=120
                min_keyint=60
                bframes=7
                rc_lookahead=25
                ;;
            "messi_1920x1080_25_1000")
                preset="slower"
                fps=25
                keyint=50
                min_keyint=25
                bframes=7
                rc_lookahead=25
                ;;
            #标准序列
            "BasketballDrive_1920x1080_50")
                preset="medium"
                fps=50
                keyint=100
                min_keyint=50
                ;;
            "BQTerrace_1920x1080_60-biaozhun")
                preset="medium"
                fps=60
                keyint=120
                min_keyint=60
                ;;
            "Cactus_1920x1080_50")
                preset="medium"
                fps=50
                keyint=100
                min_keyint=50
                ;;
            "Kimono_1920x1080_24")
                preset="medium"
                fps=24
                keyint=48
                min_keyint=24
                ;;
            "ParkScene_1920x1080_24")
                preset="medium"
                fps=24
                keyint=48
                min_keyint=24
                ;;
        esac

        for i in $(seq 1 $NUM_ROUTES); do
            cpu_range=$(calculate_cpu_range $i $CPU_BINDING_TYPE $MACHINE_TYPE)

            echo "Running encoding for $video with bitrate ${bitrate}k and CPU range ${cpu_range}"
            echo "nohup taskset -c $cpu_range $x265 --preset $preset --input $video --input-res 1920x1080 --fps $fps --frames $frames --keyint $keyint --min-keyint $min_keyint --bframes $bframes --bitrate $bitrate --vbv-maxrate $bitrate --vbv-bufsize $bitrate --rc-lookahead $rc_lookahead --frame-threads $frame_threads --pools $pools --lookahead-threads $lookahead_threads -o \"$outdir/${base_name}_${bitrate}k_${i}.bin\" > \"$outdir/log_${base_name}_${bitrate}k_${i}.txt\" 2>&1 &"

            nohup taskset -c $cpu_range $x265 --preset $preset --input $video --input-res 1920x1080 --fps $fps --frames $frames \
            --keyint $keyint --min-keyint $min_keyint --bframes $bframes --bitrate $bitrate --vbv-maxrate $bitrate --vbv-bufsize $bitrate --rc-lookahead $rc_lookahead \
            --frame-threads $frame_threads --pools $pools --lookahead-threads $lookahead_threads -o "$outdir/${base_name}_${bitrate}k_${i}.bin" > "$outdir/log_${base_name}_${bitrate}k_${i}.txt" 2>&1 &

            pid_array+=($!)  # 存储进程 ID
        done
        wait "${pid_array[@]}"  # 等待所有进程完成
        pid_array=()  # 清空进程 ID 数组
    done
done
