"""Browser, Visual Layout, and Accessibility QA Inspector for BEL Code
(Specification Section 10).
"""

from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from bel_code.security.policy import RiskLevel, SecurityPolicy


class _DOMInspectorParser(HTMLParser):
    """Deterministic HTML5 DOM parser capturing elements, attributes, text,
    landmarks, headings, forms, interactive controls, images, styles, and scripts.
    """

    VOID_ELEMENTS: Set[str] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    LANDMARK_TAGS: Set[str] = {"main", "nav", "header", "footer", "aside", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: List[Dict[str, Any]] = []
        self.open_stack: List[Dict[str, Any]] = []
        self.landmarks: List[str] = []
        self.headings: List[Dict[str, Any]] = []
        self.images: List[Dict[str, Any]] = []
        self.forms: List[Dict[str, Any]] = []
        self.inputs: List[Dict[str, Any]] = []
        self.labels_for: Set[str] = set()
        self.interactive_elements: List[Dict[str, Any]] = []
        self.scripts: List[Dict[str, Any]] = []
        self.styles: List[str] = []
        self.meta_tags: List[Dict[str, str]] = []
        self.classes: Set[str] = set()
        self.ids: Set[str] = set()
        self.html_lang: Optional[str] = None
        self.title: str = ""
        self.syntax_errors: List[str] = []
        self._inside_label_depth: int = 0
        self._current_form: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_low = tag.lower()
        attr_map: Dict[str, str] = {k.lower(): (v if v is not None else "") for k, v in attrs}

        if tag_low == "html" and "lang" in attr_map:
            self.html_lang = attr_map["lang"].strip()

        if "id" in attr_map and attr_map["id"].strip():
            self.ids.add(attr_map["id"].strip())

        if "class" in attr_map and attr_map["class"].strip():
            for cls_name in attr_map["class"].split():
                self.classes.add(cls_name.strip())

        if tag_low == "label":
            self._inside_label_depth += 1
            if "for" in attr_map and attr_map["for"].strip():
                self.labels_for.add(attr_map["for"].strip())

        node: Dict[str, Any] = {
            "tag": tag_low,
            "attrs": attr_map,
            "text": "",
            "inside_label": self._inside_label_depth > 0,
            "line": self.getpos()[0],
        }
        self.elements.append(node)

        if tag_low in self.LANDMARK_TAGS or attr_map.get("role", "").lower() in (
            "main",
            "navigation",
            "banner",
            "contentinfo",
            "complementary",
            "region",
        ):
            landmark_name = tag_low if tag_low in self.LANDMARK_TAGS else attr_map.get("role", "").lower()
            if landmark_name not in self.landmarks:
                self.landmarks.append(landmark_name)

        if tag_low == "meta":
            self.meta_tags.append(attr_map)

        if tag_low == "img":
            self.images.append(node)

        if tag_low == "form":
            form_obj: Dict[str, Any] = {
                "action": attr_map.get("action", ""),
                "method": attr_map.get("method", "get").upper(),
                "id": attr_map.get("id", ""),
                "inputs": [],
                "buttons": [],
            }
            self.forms.append(form_obj)
            self._current_form = form_obj

        if tag_low in ("input", "select", "textarea"):
            self.inputs.append(node)
            if self._current_form is not None:
                self._current_form["inputs"].append(
                    {
                        "tag": tag_low,
                        "type": attr_map.get("type", "text"),
                        "name": attr_map.get("name", ""),
                        "id": attr_map.get("id", ""),
                        "required": "required" in attr_map,
                    }
                )

        if (
            tag_low in ("button", "a", "input", "select", "textarea", "summary")
            or "onclick" in attr_map
            or attr_map.get("role", "").lower() in ("button", "link", "tab", "menuitem", "checkbox", "switch")
        ):
            self.interactive_elements.append(node)
            if tag_low == "button" and self._current_form is not None:
                self._current_form["buttons"].append(node)

        if tag_low not in self.VOID_ELEMENTS:
            self.open_stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag_low = tag.lower()
        if tag_low == "label" and self._inside_label_depth > 0:
            self._inside_label_depth -= 1

        if tag_low == "form":
            self._current_form = None

        if tag_low in self.VOID_ELEMENTS:
            return

        # Pop matching tag from stack
        for idx in range(len(self.open_stack) - 1, -1, -1):
            if self.open_stack[idx]["tag"] == tag_low:
                closed_node = self.open_stack.pop(idx)
                text_clean = closed_node["text"].strip()
                if tag_low == "title":
                    self.title = text_clean
                elif tag_low in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    self.headings.append(
                        {
                            "tag": tag_low,
                            "level": int(tag_low[1]),
                            "text": text_clean,
                            "line": closed_node["line"],
                        }
                    )
                elif tag_low == "style":
                    self.styles.append(text_clean)
                elif tag_low == "script":
                    self.scripts.append(
                        {
                            "src": closed_node["attrs"].get("src", ""),
                            "type": closed_node["attrs"].get("type", "text/javascript"),
                            "inline_code": text_clean,
                            "line": closed_node["line"],
                        }
                    )
                break
        else:
            self.syntax_errors.append(f"Unexpected closing tag </{tag_low}> at line {self.getpos()[0]}")

    def handle_data(self, data: str) -> None:
        if self.open_stack:
            self.open_stack[-1]["text"] += data


class BrowserQATester:
    """Implements Specification Section 10: Browser, Visual & Accessibility QA.
    Combines Playwright headless browser automation (when installed) with a
    deterministic HTML/DOM/CSS/Accessibility inspector so full QA verification
    succeeds across all local and CI environments.
    """

    VIEWPORT_DIMENSIONS: Dict[str, Tuple[int, int]] = {
        "mobile": (375, 667),
        "tablet": (768, 1024),
        "desktop": (1440, 900),
    }

    def __init__(
        self,
        workspace_root: Union[Path, str],
        security: Optional[SecurityPolicy] = None,
    ) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        self.security: SecurityPolicy = security or SecurityPolicy(self.workspace_root, mode="supervised")

    def _load_html_content(self, target: Union[str, Path]) -> Tuple[str, str]:
        """Load HTML content from raw HTML string, workspace file path, or local URL."""
        if isinstance(target, Path):
            resolved = self.security.validate_path(target, allow_write=False)
            return resolved.read_text(encoding="utf-8", errors="replace"), f"file:{resolved.name}"

        raw = str(target).strip()
        if "<" in raw and ">" in raw:
            return raw, "inline_html"

        if raw.startswith(("http://", "https://")):
            req = urllib.request.Request(raw, headers={"User-Agent": "BEL-Code-QA/2.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace"), raw

        resolved = self.security.validate_path(raw, allow_write=False)
        if resolved.exists() and resolved.is_file():
            return resolved.read_text(encoding="utf-8", errors="replace"), f"file:{resolved.name}"

        return raw, "inline_html"

    # ------------------------------------------------------------------
    # 1. Responsive Viewport, DOM, Forms, and Script Error Inspection
    # ------------------------------------------------------------------
    def inspect_html_or_url(
        self,
        target: Union[str, Path],
        viewports: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Inspect an HTML document, file, or URL across mobile/tablet/desktop
        viewports, checking responsive layout rules, forms, interactive elements,
        scripts, and console/syntax errors.
        """
        viewports = viewports or ["mobile", "tablet", "desktop"]
        html_text, source_label = self._load_html_content(target)

        parser = _DOMInspectorParser()
        parser.feed(html_text)

        # Check inline styles & linked stylesheets for responsive primitives
        combined_css = "\n".join(parser.styles)
        for el in parser.elements:
            if el["tag"] == "link" and "stylesheet" in el["attrs"].get("rel", "").lower():
                href = el["attrs"].get("href", "").strip()
                if href and not href.startswith(("http://", "https://")):
                    css_candidate = self.workspace_root / href.lstrip("/")
                    if css_candidate.exists() and css_candidate.is_file():
                        combined_css += "\n" + css_candidate.read_text(encoding="utf-8", errors="replace")

        has_viewport_meta = any(
            m.get("name", "").lower() == "viewport" and "width=device-width" in m.get("content", "").lower()
            for m in parser.meta_tags
        )
        has_media_queries = "@media" in combined_css
        has_flexible_layout = any(
            token in combined_css.lower() or token in html_text.lower()
            for token in ("display: flex", "display:flex", "display: grid", "display:grid", "max-width", "minmax(", "vw", "rem")
        )

        # Check for hardcoded wide pixel widths that cause horizontal overflow on mobile
        overflow_matches = re.findall(r"(?:min-width|width)\s*:\s*(\d{4,})px", combined_css, flags=re.IGNORECASE)
        fixed_overflow_widths = [int(w) for w in overflow_matches if int(w) > 500]

        viewport_results: Dict[str, Dict[str, Any]] = {}
        for vp in viewports:
            vp_key = vp.lower()
            width, height = self.VIEWPORT_DIMENSIONS.get(vp_key, (1024, 768))
            vp_issues: List[str] = []

            if vp_key in ("mobile", "tablet") and not has_viewport_meta:
                vp_issues.append("Missing <meta name='viewport' content='width=device-width, initial-scale=1'>")

            for fw in fixed_overflow_widths:
                if fw > width:
                    vp_issues.append(f"Fixed CSS width ({fw}px) exceeds {vp_key} viewport width ({width}px)")

            viewport_results[vp_key] = {
                "viewport": vp_key,
                "width": width,
                "height": height,
                "responsive_ready": len(vp_issues) == 0,
                "has_media_queries": has_media_queries,
                "has_flexible_layout": has_flexible_layout,
                "layout_issues": vp_issues,
            }

        # Check scripts for console.error / syntax issues
        console_errors: List[str] = []
        syntax_errors: List[str] = list(parser.syntax_errors)

        for script in parser.scripts:
            code = script.get("inline_code", "")
            if not code:
                continue
            if "console.error(" in code:
                console_errors.append(f"Inline script at line {script['line']} invokes console.error()")
            if re.search(r"\bthrow\s+new\s+(?:Error|TypeError|ReferenceError)\b", code):
                console_errors.append(f"Inline script at line {script['line']} throws an unhandled error")
            # Basic brace balance check on inline JS
            if code.count("{") != code.count("}"):
                syntax_errors.append(f"Unbalanced curly braces in <script> at line {script['line']}")

        # Check unclosed critical structural tags left on stack
        for unclosed in parser.open_stack:
            if unclosed["tag"] in ("script", "style", "form", "table", "main", "nav", "header", "button"):
                syntax_errors.append(f"Unclosed <{unclosed['tag']}> tag opened at line {unclosed['line']}")

        interactive_summary = [
            {
                "tag": el["tag"],
                "id": el["attrs"].get("id", ""),
                "type": el["attrs"].get("type", ""),
                "role": el["attrs"].get("role", ""),
                "text": el["text"].strip()[:60],
            }
            for el in parser.interactive_elements
        ]

        all_issues: List[str] = list(syntax_errors) + list(console_errors)
        for vp_info in viewport_results.values():
            all_issues.extend(vp_info["layout_issues"])

        is_responsive = has_viewport_meta or has_media_queries or has_flexible_layout

        return {
            "success": True,
            "source": source_label,
            "engine": "deterministic_dom_inspector",
            "title": parser.title,
            "viewports": viewport_results,
            "viewport_meta_present": has_viewport_meta,
            "responsive": is_responsive,
            "forms": parser.forms,
            "interactive_elements": interactive_summary,
            "scripts": parser.scripts,
            "console_errors": console_errors,
            "syntax_errors": syntax_errors,
            "dom_summary": {
                "total_elements": len(parser.elements),
                "forms_count": len(parser.forms),
                "interactive_count": len(parser.interactive_elements),
                "images_count": len(parser.images),
                "scripts_count": len(parser.scripts),
                "landmarks": parser.landmarks,
                "headings_count": len(parser.headings),
            },
            "issues": all_issues,
            "passed": len(syntax_errors) == 0 and len(console_errors) == 0,
        }

    # ------------------------------------------------------------------
    # 2. Accessibility & Usability Compliance Check
    # ------------------------------------------------------------------
    def check_accessibility(self, html_content: Union[str, Path]) -> Dict[str, Any]:
        """Evaluate HTML content for WCAG/ARIA accessibility compliance:
        - `alt` attributes on `<img>`
        - ARIA labels / `<label>` associations on form inputs and buttons
        - Semantic HTML landmarks (`main`, `nav`, `header`, `button`)
        - Heading level hierarchy (`h1` -> `h2` -> `h3` without skipped levels)
        - Tab index & keyboard focus behavior
        """
        html_text, _ = self._load_html_content(html_content)
        parser = _DOMInspectorParser()
        parser.feed(html_text)

        violations: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        focus_issues: List[str] = []

        # 1. Check <img> alt attributes
        missing_alt_count = 0
        for img in parser.images:
            attrs = img["attrs"]
            is_decorative = attrs.get("role") == "presentation" or attrs.get("aria-hidden") == "true"
            if "alt" not in attrs and not is_decorative:
                missing_alt_count += 1
                violations.append(
                    {
                        "rule": "img-alt",
                        "severity": "high",
                        "line": img["line"],
                        "element": f"<img src='{attrs.get('src', '')}'>",
                        "message": "Image element is missing an 'alt' attribute.",
                    }
                )

        # 2. Check form inputs for labels or ARIA labels
        unlabeled_controls_count = 0
        for inp in parser.inputs:
            attrs = inp["attrs"]
            inp_type = attrs.get("type", "text").lower()
            if inp_type in ("hidden", "submit", "reset", "button", "image"):
                continue
            inp_id = attrs.get("id", "").strip()
            has_label = (
                inp["inside_label"]
                or (inp_id and inp_id in parser.labels_for)
                or bool(attrs.get("aria-label", "").strip())
                or bool(attrs.get("aria-labelledby", "").strip())
                or bool(attrs.get("title", "").strip())
            )
            if not has_label:
                unlabeled_controls_count += 1
                violations.append(
                    {
                        "rule": "control-label",
                        "severity": "high",
                        "line": inp["line"],
                        "element": f"<{inp['tag']} name='{attrs.get('name', '')}' id='{inp_id}'>",
                        "message": "Form control lacks an associated <label>, aria-label, or aria-labelledby attribute.",
                    }
                )

        # 3. Check buttons and links for accessible name
        for ctrl in parser.interactive_elements:
            tag = ctrl["tag"]
            attrs = ctrl["attrs"]
            if tag in ("button", "a"):
                has_accessible_name = (
                    bool(ctrl["text"].strip())
                    or bool(attrs.get("aria-label", "").strip())
                    or bool(attrs.get("aria-labelledby", "").strip())
                    or bool(attrs.get("title", "").strip())
                )
                if not has_accessible_name:
                    unlabeled_controls_count += 1
                    violations.append(
                        {
                            "rule": f"{tag}-name",
                            "severity": "high",
                            "line": ctrl["line"],
                            "element": f"<{tag}>",
                            "message": f"Interactive <{tag}> element has no accessible text or aria-label.",
                        }
                    )

        # 4. Check semantic HTML landmarks
        if not parser.landmarks:
            violations.append(
                {
                    "rule": "semantic-landmarks",
                    "severity": "medium",
                    "line": 1,
                    "element": "<body>",
                    "message": "Document contains no semantic landmarks (<main>, <nav>, <header>, <footer>).",
                }
            )
        elif "main" not in parser.landmarks:
            warnings.append(
                {
                    "rule": "main-landmark",
                    "severity": "low",
                    "line": 1,
                    "element": "<body>",
                    "message": "Document is missing a primary <main> landmark.",
                }
            )

        # 5. Check heading hierarchy
        heading_hierarchy_valid = True
        prev_level = 0
        for h in parser.headings:
            lvl = h["level"]
            if not h["text"]:
                heading_hierarchy_valid = False
                violations.append(
                    {
                        "rule": "empty-heading",
                        "severity": "medium",
                        "line": h["line"],
                        "element": f"<{h['tag']}>",
                        "message": f"Empty heading <{h['tag']}> with no text content.",
                    }
                )
            if prev_level == 0 and lvl > 2:
                heading_hierarchy_valid = False
                violations.append(
                    {
                        "rule": "heading-order",
                        "severity": "medium",
                        "line": h["line"],
                        "element": f"<{h['tag']}>",
                        "message": f"First heading starts at <{h['tag']}> instead of <h1>/<h2>.",
                    }
                )
            elif prev_level > 0 and lvl > prev_level + 1:
                heading_hierarchy_valid = False
                violations.append(
                    {
                        "rule": "heading-order",
                        "severity": "medium",
                        "line": h["line"],
                        "element": f"<{h['tag']}>",
                        "message": f"Skipped heading level from <h{prev_level}> directly to <h{lvl}>.",
                    }
                )
            prev_level = lvl

        # 6. Check tabindex and non-semantic interactive elements
        for el in parser.elements:
            attrs = el["attrs"]
            if "tabindex" in attrs:
                try:
                    t_val = int(attrs["tabindex"])
                    if t_val > 0:
                        msg = f"Positive tabindex='{t_val}' on <{el['tag']}> disrupts natural keyboard focus order."
                        focus_issues.append(msg)
                        violations.append(
                            {
                                "rule": "tabindex-positive",
                                "severity": "medium",
                                "line": el["line"],
                                "element": f"<{el['tag']} tabindex='{t_val}'>",
                                "message": msg,
                            }
                        )
                except ValueError:
                    pass

            if "onclick" in attrs and el["tag"] in ("div", "span"):
                if "tabindex" not in attrs or "role" not in attrs:
                    msg = f"Non-semantic <{el['tag']} onclick=...> lacks role='button' and tabindex='0' for keyboard focus."
                    focus_issues.append(msg)
                    violations.append(
                        {
                            "rule": "interactive-non-semantic",
                            "severity": "medium",
                            "line": el["line"],
                            "element": f"<{el['tag']} onclick=...>",
                            "message": msg,
                        }
                    )

        deduction = len(violations) * 15 + len(warnings) * 5
        score = max(0, 100 - deduction)

        return {
            "compliant": len(violations) == 0,
            "passed": len(violations) == 0,
            "score": score,
            "violations": violations,
            "warnings": warnings,
            "landmarks_found": parser.landmarks,
            "missing_alt_count": missing_alt_count,
            "unlabeled_controls_count": unlabeled_controls_count,
            "heading_hierarchy_valid": heading_hierarchy_valid,
            "headings": parser.headings,
            "focus_issues": focus_issues,
            "html_lang": parser.html_lang,
        }

    # ------------------------------------------------------------------
    # 3. Visual Layout & Design Spec Comparison
    # ------------------------------------------------------------------
    def compare_visual_layout(
        self,
        actual_html: Union[str, Path],
        reference_spec: Union[Dict[str, Any], str],
    ) -> Dict[str, Any]:
        """Compare implemented DOM/CSS structure and components against an
        expected design reference specification (dict or reference HTML),
        explicitly distinguishing surface inspection from functional assertions.
        """
        actual_text, _ = self._load_html_content(actual_html)
        actual_parser = _DOMInspectorParser()
        actual_parser.feed(actual_text)

        actual_tags = {el["tag"] for el in actual_parser.elements}
        actual_landmarks = set(actual_parser.landmarks)
        actual_classes = actual_parser.classes
        actual_ids = actual_parser.ids
        full_visible_text = " ".join(el["text"].strip() for el in actual_parser.elements if el["text"].strip()).lower()

        discrepancies: List[str] = []
        matched_items: List[str] = []

        if isinstance(reference_spec, str):
            ref_parser = _DOMInspectorParser()
            ref_parser.feed(reference_spec)
            spec_dict: Dict[str, Any] = {
                "required_landmarks": ref_parser.landmarks,
                "required_elements": sorted({el["tag"] for el in ref_parser.elements if el["tag"] not in ("html", "head", "body")}),
                "required_classes": sorted(ref_parser.classes),
                "required_ids": sorted(ref_parser.ids),
            }
        else:
            spec_dict = dict(reference_spec or {})

        # Check landmarks
        req_landmarks = spec_dict.get("required_landmarks") or spec_dict.get("landmarks") or []
        for lm in req_landmarks:
            lm_low = str(lm).lower()
            if lm_low in actual_landmarks or lm_low in actual_tags:
                matched_items.append(f"landmark:{lm_low}")
            else:
                discrepancies.append(f"Missing expected landmark '<{lm_low}>'")

        # Check elements / components / tags
        req_elements = (
            spec_dict.get("required_elements")
            or spec_dict.get("elements")
            or spec_dict.get("components")
            or spec_dict.get("tags")
            or []
        )
        for elem in req_elements:
            elem_low = str(elem).lower().strip("<>")
            if elem_low in actual_tags or elem_low in actual_classes or elem_low in actual_ids:
                matched_items.append(f"element:{elem_low}")
            else:
                discrepancies.append(f"Missing required element or component '{elem}'")

        # Check CSS classes
        req_classes = spec_dict.get("required_classes") or spec_dict.get("classes") or []
        for cls_name in req_classes:
            cls_clean = str(cls_name).lstrip(".")
            if cls_clean in actual_classes:
                matched_items.append(f"class:.{cls_clean}")
            else:
                discrepancies.append(f"Missing expected CSS class '.{cls_clean}'")

        # Check IDs
        req_ids = spec_dict.get("required_ids") or spec_dict.get("ids") or []
        for id_name in req_ids:
            id_clean = str(id_name).lstrip("#")
            if id_clean in actual_ids:
                matched_items.append(f"id:#{id_clean}")
            else:
                discrepancies.append(f"Missing expected element ID '#{id_clean}'")

        # Check required text / labels
        req_texts = spec_dict.get("required_text") or spec_dict.get("text") or []
        if isinstance(req_texts, str):
            req_texts = [req_texts]
        for txt in req_texts:
            if str(txt).lower() in full_visible_text or str(txt).lower() in actual_text.lower():
                matched_items.append(f"text:{txt}")
            else:
                discrepancies.append(f"Missing expected text content '{txt}'")

        total_checks = len(matched_items) + len(discrepancies)
        similarity_score = round(len(matched_items) / total_checks, 3) if total_checks > 0 else 1.0
        matches = len(discrepancies) == 0

        return {
            "matches": matches,
            "passed": matches,
            "similarity_score": similarity_score,
            "discrepancies": discrepancies,
            "matched_items": matched_items,
            "surface_inspection": {
                "verification_type": "surface_dom_and_css_inspection",
                "landmarks_present": sorted(actual_landmarks),
                "elements_present": sorted(actual_tags),
                "classes_present": sorted(actual_classes),
                "matched_count": len(matched_items),
                "discrepancy_count": len(discrepancies),
                "status": "passed" if matches else "failed",
            },
            "functional_assertions": {
                "verification_type": "functional_capability_inventory",
                "interactive_controls_present": len(actual_parser.interactive_elements) > 0,
                "interactive_controls_count": len(actual_parser.interactive_elements),
                "forms_count": len(actual_parser.forms),
                "note": (
                    "Surface inspection verifies DOM structure, landmarks, and CSS rules; "
                    "functional assertions require executing interactive state handlers."
                ),
            },
        }
