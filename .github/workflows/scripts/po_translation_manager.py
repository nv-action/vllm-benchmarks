#!/usr/bin/env python3
"""
PO File Translation Manager
Translate specified PO files using DeepSeek API
Simplified version without detection - only translates provided files
"""

import os
import json
import sys
import re
import argparse
import time
import shutil
from pathlib import Path
from typing import List, Dict, Optional
from openai import OpenAI


class POTranslator:
    """PO file translator for specified files only"""

    def __init__(self, api_key: Optional[str] = None):
        # Initialize DeepSeek client
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("❌ DeepSeek API key not found")
            print("Please set DEEPSEEK_API_KEY environment variable or provide api_key in code")
            sys.exit(1)
        
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com"
        )

    def translate_po_file(self, po_path: str) -> bool:
        """Translate single PO file using DeepSeek AI"""
        print(f"\n{'='*70}")
        print(f"📝 Processing: {Path(po_path).name}")
        print(f"{'='*70}")

        # Check if file exists
        po_file = Path(po_path)
        if not po_file.exists():
            print(f"❌ File not found: {po_path}")
            return False
        
        # Check if it's a PO file
        if po_file.suffix != '.po':
            print(f"❌ Not a PO file: {po_path}")
            return False

        # Create backup
        backup_path = po_path + '.backup'
        try:
            shutil.copy2(po_path, backup_path)
            print(f"📂 Backup created: {backup_path}")
        except Exception as e:
            print(f"⚠️  Failed to create backup: {e}")

        try:
            with open(po_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"❌ Failed to read file: {e}")
            return False

        file_size = len(content.split('\n'))
        print(f"📊 File size: {file_size} lines")

        try:
            # For large files, process in chunks
            if file_size > 500:
                success = self._translate_in_chunks(po_path, content)
            else:
                success = self._translate_single(po_path, content)
            
            # Restore from backup if translation failed
            if not success:
                print(f"🔄 Translation failed, restoring from backup...")
                if os.path.exists(backup_path):
                    shutil.copy2(backup_path, po_path)
                    print(f"✅ File restored to original state")
            
            # Clean up backup
            if os.path.exists(backup_path):
                os.remove(backup_path)
                
            return success
            
        except Exception as e:
            print(f"❌ Error translating {Path(po_path).name}: {str(e)}")
            # Restore from backup if exists
            if os.path.exists(backup_path):
                print(f"🔄 Restoring from backup due to exception...")
                shutil.copy2(backup_path, po_path)
                os.remove(backup_path)
            return False

    def _translate_single(self, po_path: str, content: str) -> bool:
        """Translate entire file at once"""
        prompt = self._build_translation_prompt(content)

        try:
            print("🔄 Sending request to DeepSeek API...")
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a professional technical documentation translation expert, proficient in English-Chinese technical document translation."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=8000,
                temperature=0.3
            )

            translated_content = response.choices[0].message.content
            if translated_content is None:
                print("❌ Empty response from API")
                return False
                
            translated_content = self._clean_response(translated_content)

            with open(po_path, 'w', encoding='utf-8') as f:
                f.write(translated_content)

            print(f"✅ Translation completed successfully")
            return True
        except Exception as e:
            print(f"❌ Translation failed: {str(e)}")
            return False

    def _translate_in_chunks(self, po_path: str, content: str) -> bool:
        """Translate large file in chunks - only save if fully successful"""
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
                response = self.client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "You are a professional technical documentation translation expert, proficient in English-Chinese technical document translation."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=4000,
                    temperature=0.3
                )

                translated_chunk = response.choices[0].message.content
                if translated_chunk is None:
                    print("❌ (Empty response)")
                    all_translated_lines.extend(chunk_lines)
                    failed_chunks.append(chunk_idx + 1)
                    continue
                    
                translated_chunk = self._clean_response(translated_chunk)
                all_translated_lines.extend(translated_chunk.split('\n'))
                print("✅")

            except Exception as e:
                print(f"❌ ({str(e)[:30]}...)")
                # Use original content as backup
                all_translated_lines.extend(chunk_lines)
                failed_chunks.append(chunk_idx + 1)

        # Only save if all chunks succeeded
        if failed_chunks:
            print(f"⚠️  Translation failed ({len(failed_chunks)} chunks failed)")
            print(f"   Failed chunks: {', '.join(map(str, failed_chunks))}")
            return False
        
        # Save results - only if fully successful
        final_content = '\n'.join(all_translated_lines)
        try:
            with open(po_path, 'w', encoding='utf-8') as f:
                f.write(final_content)
            print(f"✅ Fully translated ({total_chunks} chunks)")
            return True
        except Exception as e:
            print(f"❌ Failed to write file: {e}")
            return False

    def _build_translation_prompt(self, content: str, chunk_num: Optional[int] = None,
                                 total_chunks: Optional[int] = None) -> str:
        """Build translation prompt"""
        chunk_info = ""
        if chunk_num and total_chunks:
            chunk_info = f"\n\n【This is chunk {chunk_num}/{total_chunks}】"

        return f"""You are a professional technical documentation translation expert. I need your help translating a Sphinx documentation PO file (gettext format).

【Translation Rules】
1. Only modify content in msgstr "", keep msgid completely unchanged
2. Preserve all format markers: %s, %d, {{}}, **, *, `, etc.
3. Keep code blocks, code references, variable names unchanged (e.g., `code`, `variable`)
4. For already translated parts (msgstr not empty), supplement and optimize, maintaining consistent style
5. Maintain complete PO file format and structure
6. Use standard Chinese technical terminology:
   - function → 函数
   - parameter → 参数
   - argument → 参数/传参
   - documentation → 文档
   - tutorial → 教程
   - API → API/接口
   - module → 模块
   - class → 类
7. Use concise, professional Chinese expression
8. For difficult-to-understand parts, keep original English rather than forcing translation
9. Remove "#, fuzzy" to ensure display

【Important Notes】
- Return complete and correctly formatted PO file content
- Do not add any extra explanations or comments
- Ensure correct line breaks and escape characters in msgstr

【PO File Content】{chunk_info}

{content}

【Output Requirements】
Please return the modified complete PO file content, maintaining the same format."""

    def _clean_response(self, response: str) -> str:
        """Clean markdown markers from AI response"""
        response = response.strip()

        # Remove markdown code block markers
        if response.startswith('```'):
            lines = response.split('\n')
            # Remove opening triple backticks and any language marker
            if lines[0].startswith('```'):
                lines = lines[1:]
            # Remove closing triple backticks
            while lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            response = '\n'.join(lines).strip()

        return response

    def generate_report(self, success_files: List[str]) -> str:
        """Generate translation report with only success files"""
        report = []
        report.append("\n" + "="*70)
        report.append("📊 TRANSLATION REPORT")
        report.append("="*70)

        if success_files:
            report.append(f"\n✅ Successfully translated: {len(success_files)} file(s)")
            for file_path in success_files:
                try:
                    file_size = Path(file_path).stat().st_size
                    report.append(f"   • {Path(file_path).name} ({file_size} bytes)")
                except:
                    report.append(f"   • {Path(file_path).name}")
        else:
            report.append(f"\n❌ No files were successfully translated")

        report.append("\n" + "="*70 + "\n")
        return '\n'.join(report)


def main():
    """Main function - simplified for GitHub Actions workflow"""
    parser = argparse.ArgumentParser(
        description='PO File Translator - Translate specified PO files using DeepSeek API'
    )
    
    parser.add_argument(
        '--files',
        type=str,
        required=True,
        help='Comma-separated list of PO file paths to translate'
    )
    
    parser.add_argument(
        '--output-json',
        type=str,
        default=os.getenv('OUTPUT_JSON', '/tmp/translation_results.json'),
        help='Path to save translation results as JSON'
    )
    
    parser.add_argument(
        '--api-key',
        type=str,
        default=os.getenv('DEEPSEEK_API_KEY'),
        help='DeepSeek API key (or set DEEPSEEK_API_KEY environment variable)'
    )
    

    args = parser.parse_args()

    # Parse file list
    if not args.files:
        print("❌ No files specified. Use --files to provide comma-separated list of PO files")
        sys.exit(1)
    
    file_list = [f.strip() for f in args.files.split(',') if f.strip()][1:3]
    
    
    print("🚀 Starting PO File Translator (DeepSeek version)")
    print(f"📋 Files to translate: {len(file_list)}")
    
    for i, file_path in enumerate(file_list, 1):
        print(f"  {i}. {file_path}")

    translator = POTranslator(api_key=args.api_key)

    print(f"\n🔄 Starting translation of {len(file_list)} file(s)...")

    success_files = []
    
    for file_path in file_list:
        success = translator.translate_po_file(file_path)
        if success:
            success_files.append(file_path)
        time.sleep(1)  # Avoid API rate limiting

    # Generate report
    report = translator.generate_report(success_files)
    print(report)

    # Save results
    results = {
        'success_files': success_files,
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total_files': len(file_list),
        'success_count': len(success_files)
    }
    
    _save_results(results, args.output_json)

    if not success_files:
        print(f"\n⚠️  No files were successfully translated")
        return 1

    return 0


def _save_results(results: Dict, output_path: str) -> None:
    """Save results to JSON file"""
    try:
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Results saved to: {output_path}")
        print(f"✅ Successfully translated {len(results['success_files'])} file(s)")
    except Exception as e:
        print(f"⚠️  Failed to save results: {e}")


if __name__ == '__main__':
    sys.exit(main())
