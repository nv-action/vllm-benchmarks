#!/usr/bin/env python3
"""
PO File Translation Manager
用于翻译指定的 PO 文件，由外部 (Workflow) 控制要翻译的文件列表
"""

import os
import json
import sys
import re
import argparse
import time
from pathlib import Path
from typing import List, Dict, Optional
from anthropic import Anthropic


class POFileManager:
    """PO文件管理器"""
    
    def __init__(self, po_dir: str = "docs/locale/zh_CN/LC_MESSAGES"):
        self.po_dir = Path(po_dir)
        self.po_files: List[Dict] = []
        self.client = Anthropic()
    
    def detect_po_files(self, file_list: Optional[List[str]] = None) -> List[Dict]:
        """
        检测PO文件
        
        Args:
            file_list: 如果提供，只处理这些文件；否则处理目录中的所有文件
        """
        if not self.po_dir.exists():
            print(f"⚠️  PO directory not found: {self.po_dir}")
            return []
        
        po_files = []
        
        if file_list:
            # 只处理指定的文件
            print(f"📄 Processing {len(file_list)} specified file(s)")
            for file_path in file_list:
                po_file = Path(file_path)
                if po_file.exists() and po_file.suffix == '.po':
                    file_info = {
                        'path': str(po_file),
                        'name': po_file.stem,
                        'size': po_file.stat().st_size,
                        'needs_translation': self._has_untranslated_entries(po_file)
                    }
                    po_files.append(file_info)
                elif not po_file.exists():
                    print(f"⚠️  File not found: {file_path}")
        else:
            # 处理目录中的所有 .po 文件
            print(f"📄 Scanning directory: {self.po_dir}")
            for po_file in self.po_dir.glob('*.po'):
                file_info = {
                    'path': str(po_file),
                    'name': po_file.stem,
                    'size': po_file.stat().st_size,
                    'needs_translation': self._has_untranslated_entries(po_file)
                }
                po_files.append(file_info)
        
        self.po_files = sorted(po_files, key=lambda x: x['size'], reverse=True)
        return self.po_files
    
    def _has_untranslated_entries(self, po_file: Path) -> bool:
        """检查PO文件是否有未翻译的条目"""
        try:
            with open(po_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 查找空的msgstr条目
            pattern = r'msgstr\s+""\s*$'
            return bool(re.search(pattern, content, re.MULTILINE))
        except Exception as e:
            print(f"⚠️  Error reading {po_file}: {e}")
            return False
    
    def translate_po_file(self, po_path: str) -> bool:
        """使用Claude AI翻译单个PO文件"""
        print(f"\n{'='*70}")
        print(f"📝 Processing: {Path(po_path).name}")
        print(f"{'='*70}")
        
        try:
            with open(po_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"❌ Failed to read file: {e}")
            return False
        
        file_size = len(content.split('\n'))
        print(f"📊 File size: {file_size} lines")
        
        try:
            # 对于大文件分块处理
            if file_size > 500:
                return self._translate_in_chunks(po_path, content)
            else:
                return self._translate_single(po_path, content)
        except Exception as e:
            print(f"❌ Error translating {Path(po_path).name}: {str(e)}")
            return False
    
    def _translate_single(self, po_path: str, content: str) -> bool:
        """一次性翻译整个文件"""
        prompt = self._build_translation_prompt(content)
        
        try:
            print("🔄 Sending request to Claude API...")
            response = self.client.messages.create(
                model="claude-opus-4-5-20251101",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            
            translated_content = response.content[0].text
            translated_content = self._clean_response(translated_content)
            
            with open(po_path, 'w', encoding='utf-8') as f:
                f.write(translated_content)
            
            print(f"✅ Translation completed successfully")
            return True
        except Exception as e:
            print(f"❌ Translation failed: {str(e)}")
            return False
    
    def _translate_in_chunks(self, po_path: str, content: str) -> bool:
        """分块翻译大文件"""
        lines = content.split('\n')
        chunk_size = 100
        total_chunks = (len(lines) + chunk_size - 1) // chunk_size
        
        print(f"📦 Large file detected. Processing in {total_chunks} chunks...")
        print(f"⏱️  Estimated time: ~{total_chunks * 2} seconds")
        
        all_translated_lines = []
        failed_chunks = []
        
        for chunk_idx in range(total_chunks):
            start = chunk_idx * chunk_size
            end = min((chunk_idx + 1) * chunk_size, len(lines))
            chunk_lines = lines[start:end]
            chunk_content = '\n'.join(chunk_lines)
            
            prompt = self._build_translation_prompt(
                chunk_content,
                chunk_idx + 1,
                total_chunks
            )
            
            try:
                print(f"  🔄 Chunk {chunk_idx + 1}/{total_chunks}...", end=" ", flush=True)
                response = self.client.messages.create(
                    model="claude-opus-4-5-20251101",
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}]
                )
                
                translated_chunk = response.content[0].text
                translated_chunk = self._clean_response(translated_chunk)
                
                all_translated_lines.extend(translated_chunk.split('\n'))
                print("✅")
                
            except Exception as e:
                print(f"❌ ({str(e)[:30]}...)")
                # 使用原始内容作为备份
                all_translated_lines.extend(chunk_lines)
                failed_chunks.append(chunk_idx + 1)
        
        # 保存结果
        final_content = '\n'.join(all_translated_lines)
        try:
            with open(po_path, 'w', encoding='utf-8') as f:
                f.write(final_content)
        except Exception as e:
            print(f"❌ Failed to write file: {e}")
            return False
        
        if failed_chunks:
            print(f"⚠️  Partially translated ({len(failed_chunks)} chunks failed)")
            print(f"   Failed chunks: {', '.join(map(str, failed_chunks))}")
            return False
        else:
            print(f"✅ Fully translated ({total_chunks} chunks)")
            return True
    
    def _build_translation_prompt(self, content: str, chunk_num: Optional[int] = None, 
                                 total_chunks: Optional[int] = None) -> str:
        """构建翻译提示词"""
        chunk_info = ""
        if chunk_num and total_chunks:
            chunk_info = f"\n\n【这是第 {chunk_num}/{total_chunks} 块内容】"
        
        return f"""你是一位专业的技术文档翻译专家。我需要你帮助翻译一个Sphinx文档的PO文件（gettext格式）。

【翻译规则】
1. 只修改 msgstr "" 中的内容，保持msgid完全不变
2. 保留所有格式标记：%s、%d、{{}}、**、*、`、等
3. 保持代码块、代码引用、变量名不变（如 `code`、`variable`）
4. 对于已翻译部分（msgstr 不为空），进行补充和优化，保持风格一致
5. 维持PO文件的完整格式和结构
6. 使用标准的中文技术术语：
   - function → 函数
   - parameter → 参数
   - argument → 参数/传参
   - documentation → 文档
   - tutorial → 教程
   - API → API/接口
   - module → 模块
   - class → 类
7. 保持简洁、专业的中文表达
8. 对于难以理解的部分，宁可保留原英文也不要硬译

【重要提示】
- 返回完整且格式正确的PO文件内容
- 不要添加任何额外的解释或注释
- 确保msgstr中的换行符和转义符正确

【PO文件内容】{chunk_info}

{content}

【输出要求】
请返回修改后的完整PO文件内容，保持相同的格式。"""
    
    def _clean_response(self, response: str) -> str:
        """清理AI响应中的markdown标记"""
        response = response.strip()
        
        # 移除markdown代码块标记
        if response.startswith('```'):
            lines = response.split('\n')
            # 移除开头的三反引号及后面可能的语言标记
            if lines[0].startswith('```'):
                lines = lines[1:]
            # 移除末尾的三反引号
            while lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            response = '\n'.join(lines).strip()
        
        return response
    
    def generate_report(self, translated_files: List[str], failed_files: List[str]) -> str:
        """生成翻译报告"""
        report = []
        report.append("\n" + "="*70)
        report.append("📊 TRANSLATION REPORT")
        report.append("="*70)
        
        if translated_files:
            report.append(f"\n✅ Successfully translated: {len(translated_files)} file(s)")
            for file_path in translated_files:
                try:
                    file_size = Path(file_path).stat().st_size
                    report.append(f"   • {Path(file_path).name} ({file_size} bytes)")
                except:
                    report.append(f"   • {Path(file_path).name}")
        
        if failed_files:
            report.append(f"\n❌ Failed: {len(failed_files)} file(s)")
            for file_name in failed_files:
                report.append(f"   • {file_name}")
        
        report.append("\n" + "="*70 + "\n")
        return '\n'.join(report)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='PO File Translation Manager - Translate specified PO files'
    )
    parser.add_argument(
        '--detect-only',
        action='store_true',
        help='Only detect PO files without translating'
    )
    parser.add_argument(
        '--translate',
        action='store_true',
        help='Perform translation on detected PO files'
    )
    parser.add_argument(
        '--files',
        type=str,
        help='Comma-separated list of PO files to process (from workflow)'
    )
    parser.add_argument(
        '--po-dir',
        type=str,
        default=os.getenv('PO_DIR', 'docs/locale/zh_CN/LC_MESSAGES'),
        help='Directory containing PO files'
    )
    parser.add_argument(
        '--output-json',
        type=str,
        default=os.getenv('OUTPUT_JSON', '/tmp/translation_results.json'),
        help='Path to save translation results as JSON'
    )
    
    args = parser.parse_args()
    
    # 默认行为：检测 + 翻译
    detect_only = args.detect_only
    do_translate = args.translate or not args.detect_only
    po_dir = args.po_dir
    output_json = args.output_json
    
    # 解析文件列表
    file_list = None
    if args.files:
        file_list = [f.strip() for f in args.files.split(',')]
    
    print("🚀 Starting PO File Translation Manager")
    print(f"📁 PO Directory: {po_dir}")
    mode = "Detect Only" if detect_only else "Detect + Translate"
    if file_list:
        print(f"📋 Files specified: {len(file_list)}")
    print(f"🔍 Mode: {mode}")
    
    manager = POFileManager(po_dir)
    
    # 检测PO文件
    po_files = manager.detect_po_files(file_list=file_list)
    
    if not po_files:
        print("⚠️  No PO files found")
        results = {
            'detected': [],
            'translated': [],
            'failed': [],
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        _save_results(results, output_json)
        return 0
    
    print(f"\n📄 Found {len(po_files)} PO file(s):\n")
    detected_files = []
    for i, file_info in enumerate(po_files, 1):
        status = "⚠️ needs translation" if file_info['needs_translation'] else "✅ complete"
        print(f"  {i}. {file_info['name']}.po ({file_info['size']} bytes) - {status}")
        detected_files.append(file_info['path'])
    
    # 仅检测模式
    if detect_only:
        print("\n✅ Detection completed")
        results = {
            'detected': detected_files,
            'translated': [],
            'failed': [],
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        _save_results(results, output_json)
        return 0
    
    # 翻译模式
    files_to_translate = [f for f in po_files if f['needs_translation']]
    
    if not files_to_translate:
        print("\n✅ All PO files are already translated!")
        results = {
            'detected': detected_files,
            'translated': [],
            'failed': [],
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        _save_results(results, output_json)
        return 0
    
    print(f"\n🔄 Starting translation of {len(files_to_translate)} file(s)...")
    
    translated_files = []
    failed_files = []
    
    for file_info in files_to_translate:
        success = manager.translate_po_file(file_info['path'])
        if success:
            translated_files.append(file_info['path'])
        else:
            failed_files.append(file_info['name'])
        time.sleep(1)  # 避免API速率限制
    
    # 生成报告
    report = manager.generate_report(translated_files, failed_files)
    print(report)
    
    # 保存结果
    results = {
        'detected': detected_files,
        'translated': translated_files,
        'failed': failed_files,
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    _save_results(results, output_json)
    
    if failed_files:
        print(f"\n⚠️  {len(failed_files)} file(s) failed to translate")
        return 1
    
    return 0


def _save_results(results: Dict, output_path: str) -> None:
    """保存结果到JSON文件"""
    try:
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Results saved to: {output_path}")
    except Exception as e:
        print(f"⚠️  Failed to save results: {e}")


if __name__ == '__main__':
    sys.exit(main())
