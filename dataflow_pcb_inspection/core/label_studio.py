"""Offline Label Studio v1 rectangle/choice/textarea conversion. No API client."""
from __future__ import annotations

from xml.sax.saxutils import quoteattr

from .annotation import validate_prediction
from .review import review_packet


def to_label_studio(annotation, *, categories=()):
    packet = review_packet(annotation)
    sample = packet["sample"]
    choices = tuple(categories) or tuple(sorted({f["category"] for f in packet["prediction"]["findings"]}))
    if not choices or any(not isinstance(x, str) or not x.strip() for x in choices):
        raise ValueError("Label Studio requires a non-empty category vocabulary, including for normal samples")
    validate_prediction(packet["prediction"], sample, choices)
    data, controls, by_id, xml = {}, {}, {}, ["<View>"]
    for index, image in enumerate(sample["images"]):
        name = f"image_{index}"
        data[name] = image["path"]
        by_id[image["id"]] = (name, f"labels_{index}", image)
        xml.append(f'<Header value={quoteattr(image["role"] + ":" + image["id"])}/>')
        xml.append(f'<Image name="{name}" value="${name}"/>')
        if image["role"] == "inspection":
            control = f"labels_{index}"
            controls[control] = image["id"]
            xml.append(f'<RectangleLabels name="{control}" toName="{name}">')
            for choice in choices:
                xml.append(f'<Label value={quoteattr(choice)}/>')
            xml.append("</RectangleLabels>")
    first = next(name for name, _, im in by_id.values() if im["role"] == "inspection")
    xml += [f'<Choices name="verdict" toName="{first}" choice="single">',
            '<Choice value="defect"/><Choice value="normal"/><Choice value="uncertain"/></Choices>',
            f'<TextArea name="note" toName="{first}"/>', "</View>"]
    results = [{"id": "verdict", "from_name": "verdict", "to_name": first, "type": "choices",
                "value": {"choices": [packet["prediction"]["verdict"]]}},
               {"id": "note", "from_name": "note", "to_name": first, "type": "textarea",
                "value": {"text": [packet["prediction"]["note"]]}}]
    for index, finding in enumerate(packet["prediction"]["findings"]):
        name, control, image = by_id[finding["image_id"]]
        x1, y1, x2, y2 = finding["bbox_xyxy"]
        w, h = image["width"], image["height"]
        results.append({"id": f"region_{index}", "from_name": control, "to_name": name,
                        "type": "rectanglelabels", "original_width": w, "original_height": h,
                        "image_rotation": 0, "meta": {"note": finding["note"]},
                        "value": {"x": x1/w*100, "y": y1/h*100,
                                  "width": (x2-x1)/w*100, "height": (y2-y1)/h*100,
                                  "rotation": 0, "rectanglelabels": [finding["category"]]}})
    return {"data": data, "meta": {"pcb_review": packet, "controls": controls,
                                   "categories": list(choices), "first_inspection": first},
            "predictions": [{"model_version": packet["binding"], "result": results}],
            "label_config": "".join(xml)}


def from_label_studio(task, *, reviewer):
    """One completed annotation becomes a correct decision for the original packet."""
    packet = task["meta"]["pcb_review"]
    annotations = [x for x in task.get("annotations", []) if not x.get("was_cancelled")]
    if len(annotations) != 1:
        raise ValueError("v1 requires exactly one completed human annotation")
    human = annotations[0]
    if "id" not in human:
        raise ValueError("human annotation must have a stable id")
    by_id = {x["id"]: x for x in packet["sample"]["images"]}
    controls = task["meta"]["controls"]
    first = task["meta"]["first_inspection"]
    prediction, seen = {"findings": []}, set()
    for result in human["result"]:
        kind, name, value = result["type"], result["from_name"], result["value"]
        if kind in {"choices", "textarea"}:
            expected = "verdict" if kind == "choices" else "note"
            if name != expected or result.get("to_name") != first or name in seen:
                raise ValueError("unsupported or duplicate classification/text control")
            seen.add(name)
            values = value.get("choices" if kind == "choices" else "text", [])
            if len(values) != 1:
                raise ValueError("verdict/note must contain exactly one value")
            prediction[name] = values[0]
        elif kind == "rectanglelabels":
            if name not in controls:
                raise ValueError("unknown rectangle control")
            image = by_id[controls[name]]
            index = packet["sample"]["images"].index(image)
            if result.get("to_name") != f"image_{index}" or value.get("rotation", 0) != 0 or result.get("image_rotation", 0) != 0:
                raise ValueError("rotated or mismatched rectangles are unsupported")
            if result.get("original_width") != image["width"] or result.get("original_height") != image["height"]:
                raise ValueError("Label Studio dimensions differ from original image")
            labels = value["rectanglelabels"]
            if len(labels) != 1:
                raise ValueError("exactly one category is required per rectangle")
            x1, y1 = value["x"]/100*image["width"], value["y"]/100*image["height"]
            x2 = x1 + value["width"]/100*image["width"]
            y2 = y1 + value["height"]/100*image["height"]
            prediction["findings"].append({"image_id": image["id"], "category": labels[0],
                "bbox_xyxy": [round(v, 10) for v in (x1, y1, x2, y2)],
                "note": result.get("meta", {}).get("note", "")})
        else:
            raise ValueError(f"unsupported Label Studio result type: {kind}")
    validate_prediction(prediction, packet["sample"], task["meta"].get("categories", ()))
    return {"sample_id": packet["sample_id"], "binding": packet["binding"],
            "decision_id": f"label-studio:{human['id']}", "decision": "correct",
            "reviewer": reviewer, "reason": "Imported completed Label Studio annotation",
            "annotation": prediction}
