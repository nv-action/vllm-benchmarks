#!/bin/bash

# 定义码率文件夹数组
bitrates=("2000k" "4000k" "6000k" "8000k")

# 定义视频文件夹名称数组，按给定顺序
video_dirs_order=("Dota2_1920x1080_60"
"xihuanni_1920x1080_30_1000"
"showSingText_1920x1080_40"
"1_Yuanshen_1920x1080_60_8bit_12M"
"BQTerrace_1920x1080_60"
"Film2012_1920x1080_25_8bit_8Mbps"
"BQTerrace_1920x1080_60-dianbo"
"messi_1920x1080_25_1000"
"BasketballDrive_1920x1080_50"
"BQTerrace_1920x1080_60-biaozhun"
"Cactus_1920x1080_50"
"Kimono_1920x1080_24"
"ParkScene_1920x1080_24")

# 创建 CSV 文件并写入表头
output_file="video_fps_summary_x265.csv"
echo "Bitrate,Video Directory,Total FPS,Average Bin Size" > "$output_file"
OUTDIR_BASE="$TEST_HOME/outputfiles-x265"

# 遍历预定义顺序的视频文件夹名称
for video_dir_name in "${video_dirs_order[@]}"; do
    for bitrate_dir in "${bitrates[@]}"; do
        video_dir="${OUTDIR_BASE}/${bitrate_dir}/${video_dir_name}"
        if [[ -d "$video_dir" ]]; then
            echo "Processing directory: $video_dir"

            # 初始化视频文件夹总和变量和 .bin 文件总大小变量
            total_sum=0
            total_size=0
            bin_count=0

            # 遍历视频文件夹中的所有 txt 文件
            for file in "$video_dir"/*.txt; do
                if [[ -f "$file" ]]; then
                    # 获取最后一行
                    line=$(tail -n 1 "$file")

                    # 提取括号中的 FPS 值
                    fps_value=$(echo "$line" | grep -oP '(?<=\().*?(?= fps\))')

                    # 检查 fps_value 是否为空
                    if [[ -z "$fps_value" ]]; then
                        fps_value=0
                    fi

                    # 累加到总和中
                    total_sum=$(echo "$total_sum + $fps_value" | bc -l)
                fi
            done

            # 遍历视频文件夹中的所有 bin 文件
            for bin_file in "$video_dir"/*.bin; do
                if [[ -f "$bin_file" ]]; then
                    # 获取 bin 文件大小
                    bin_size=$(stat -c%s "$bin_file")

                    # 累加到 bin 文件总大小中
                    total_size=$(echo "$total_size + $bin_size" | bc -l)
                    bin_count=$((bin_count + 1))
                fi
            done

            # 计算 bin 文件平均大小
            if [[ $bin_count -ne 0 ]]; then
                avg_bin_size=$(echo "$total_size / $bin_count" | bc -l)
            else
                avg_bin_size=0
            fi

            # 写入 CSV 文件
            echo "$bitrate_dir,$video_dir_name,$total_sum,$avg_bin_size" >> "$output_file"
        else
            echo "Directory $video_dir does not exist."
        fi
    done
done
