from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional
import json
import re

from pdfminer.pdfparser import PDFParser, PDFSyntaxError
from pdfminer.pdfdocument import PDFDocument, PDFNoOutlines
from pdfminer.pdfpage import PDFPage, LITERAL_PAGE
from pdfminer.pdftypes import PDFObjRef


class PDFRefType(Enum):
    PDF_OBJ_REF = auto()
    DICTIONARY = auto()
    LIST = auto()
    NAMED_REF = auto()
    UNK = auto()


class RefPageNumberResolver:
    """Resolve PDF references (dest, a, se) to page numbers."""

    def __init__(self, document: PDFDocument):
        self.document = document
        # Map page object ID to its page number (1-based)
        self.objid_to_pagenum = {
            page.pageid: page_num
            for page_num, page in enumerate(PDFPage.create_pages(document), 1)
        }
        self.total_pages = len(self.objid_to_pagenum)

    @classmethod
    def get_ref_type(cls, ref: Any) -> PDFRefType:
        if isinstance(ref, PDFObjRef):
            return PDFRefType.PDF_OBJ_REF
        elif isinstance(ref, dict) and "D" in ref:
            return PDFRefType.DICTIONARY
        elif isinstance(ref, list) and any(isinstance(e, PDFObjRef) for e in ref):
            return PDFRefType.LIST
        elif isinstance(ref, bytes):
            return PDFRefType.NAMED_REF
        else:
            return PDFRefType.UNK

    @classmethod
    def is_ref_page(cls, ref: Any) -> bool:
        return isinstance(ref, dict) and "Type" in ref and ref["Type"] is LITERAL_PAGE

    def resolve(self, ref: Any) -> Optional[int]:
        ref_type = self.get_ref_type(ref)

        # PDFObjRef to page object
        if ref_type is PDFRefType.PDF_OBJ_REF:
            resolved = ref.resolve()
            if self.is_ref_page(resolved):
                return self.objid_to_pagenum.get(ref.objid)
            return self.resolve(resolved)

        # Dictionary destination (usually {"D": …})
        if ref_type is PDFRefType.DICTIONARY:
            return self.resolve(ref["D"])

        # List of references (first PDFObjRef is used)
        if ref_type is PDFRefType.LIST:
            return self.resolve(
                next(filter(lambda e: isinstance(e, PDFObjRef), ref), None)
            )

        # Named destination (bytes name)
        if ref_type is PDFRefType.NAMED_REF:
            return self.resolve(self.document.get_dest(ref))

        return None  # Unknown type


def clean_title(title: str, numbered_pattern: re.Pattern) -> str:
    """Strip numbering prefix from title if present."""
    match = numbered_pattern.match(title.strip())
    if match:
        return title.strip()[match.end():].strip()
    return title.strip()


def extract_and_save_toc_json(pdf_path: str, output_path: str = "toc.json") -> None:
    """Extract the PDF TOC with start/end pages in nested structure and save as JSON."""
    with open(pdf_path, "rb") as fp:
        try:
            parser = PDFParser(fp)
            document = PDFDocument(parser)
            resolver = RefPageNumberResolver(document)

            try:
                outlines = list(document.get_outlines())
            except PDFNoOutlines:
                with open(output_path, "w") as f:
                    json.dump({}, f, indent=4)
                print(f"No TOC found. Empty {output_path} saved.")
                return

            numbered_pattern = re.compile(r'^\d+(\.\d+)*\s')
            exclude_set = {
                'Contents', 'Preface', 'References', 'Index', 'Blank Page',
                'Glossary', 'Appendix', 'Acknowledgments', 'About the Author'
            }  # Add more as needed
            entries = []
            for level, title, dest, a, se in outlines:
                # Resolve page from dest, a, or se (in priority order)
                page_num = None
                if dest:
                    page_num = resolver.resolve(dest)
                elif a:
                    page_num = resolver.resolve(a)
                elif se:
                    page_num = resolver.resolve(se)

                clean_t = clean_title(title, numbered_pattern)
                # Include if numbered (for >1) or level 1 and not excluded
                is_numbered = numbered_pattern.match(title.strip())
                if (page_num is not None and
                    ((level == 1 and clean_t not in exclude_set) or (level > 1 and is_numbered))):
                    entries.append({
                        'level': level,
                        'title': title,
                        'start_page': page_num,
                        'end_page': None
                    })

            if not entries:
                with open(output_path, "w") as f:
                    json.dump({}, f, indent=4)
                print(f"No valid TOC entries with pages. Empty {output_path} saved.")
                return

            # Compute end_pages using stack-based algorithm for nested structure
            total_pages = resolver.total_pages
            dummy = {'level': 0, 'start_page': total_pages + 1, 'title': None, 'end_page': None}
            stack = []

            for entry in entries + [dummy]:
                while stack and stack[-1]['level'] >= entry['level']:
                    popped = stack.pop()
                    popped['end_page'] = entry['start_page'] - 1
                if entry['title'] is not None:
                    stack.append(entry)

            # Build nested dict structure
            root = {}
            tree_stack = [(root, 0)]
            for entry in entries:
                level = entry['level']
                while len(tree_stack) > 1 and tree_stack[-1][1] >= level:  # >1 to keep root
                    tree_stack.pop()
                parent_dict, parent_lev = tree_stack[-1]
                clean_t = clean_title(entry['title'], numbered_pattern)
                node = {
                    'page_start': entry['start_page'],
                    'page_end': entry['end_page'],
                    'subsections': {}
                }
                parent_dict[clean_t] = node
                tree_stack.append((node['subsections'], level))

            # Save to JSON
            with open(output_path, "w") as f:
                json.dump(root, f, indent=4)
            print(f"Nested TOC saved to {output_path} with {len(entries)} entries.")

        except PDFSyntaxError:
            print("Invalid or corrupted PDF.")
        finally:
            if 'parser' in locals():
                parser.close()


# Usage
if __name__ == "__main__":
    pdf_file = "./dataset/book.pdf"  # Replace with your PDF
    extract_and_save_toc_json(pdf_file)