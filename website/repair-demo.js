(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const sourceCanvas = $("repair-source-canvas");
  const resultCanvas = $("repair-result-canvas");
  const sourceContext = sourceCanvas.getContext("2d", { willReadFrequently: true });
  const resultContext = resultCanvas.getContext("2d");
  const dialog = $("gemini-key-dialog");

  const state = {
    apiKey: "",
    image: null,
    filename: "",
    bbox: null,
    dragStart: null,
    mode: "remove",
    busy: false,
    hasResult: false,
  };

  function setStatus(message, kind = "") {
    const status = $("repair-demo-status");
    status.textContent = message;
    status.classList.toggle("error", kind === "error");
    status.classList.toggle("success", kind === "success");
  }

  function updateRunState() {
    $("repair-run-button").disabled = state.busy || !state.image || !state.bbox || !state.apiKey;
    $("repair-clear-box").disabled = !state.bbox || state.busy;
    $("repair-download").disabled = !state.hasResult || state.busy;
  }

  function updateConnectionState() {
    const connection = document.querySelector(".gemini-connection");
    connection.classList.toggle("connected", Boolean(state.apiKey));
    $("gemini-connection-label").textContent = state.apiKey ? "Gemini connected" : "Gemini not connected";
    $("gemini-connection-note").textContent = state.apiKey
      ? "Available until this tab is refreshed or closed."
      : "Key is kept in memory for this tab only.";
    $("gemini-connect-button").textContent = state.apiKey ? "Manage key" : "Connect Gemini";
    $("gemini-disconnect-button").hidden = !state.apiKey;
    updateRunState();
  }

  function setMode(mode) {
    state.mode = mode === "swap" ? "swap" : "remove";
    document.querySelectorAll(".repair-mode-switch button").forEach((button) => {
      const active = button.dataset.mode === state.mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("repair-target-field").hidden = state.mode !== "swap";
    $("repair-source-label").textContent = state.mode === "swap" ? "Source entity" : "Entity to remove";
    $("repair-source-entity").placeholder = state.mode === "swap" ? "e.g. red mug" : "e.g. traffic cone";
    $("repair-run-button").querySelector("span").textContent = state.mode === "swap"
      ? "Run localized swap"
      : "Run localized removal";
    if (state.image && state.bbox) {
      setStatus(`${state.mode === "swap" ? "Swap" : "Removal"} region ready. Connect Gemini and run the edit.`);
    }
  }

  function fitDimensions(width, height) {
    const maxEdge = 2048;
    const maxPixels = 4_000_000;
    const edgeScale = Math.min(1, maxEdge / Math.max(width, height));
    const pixelScale = Math.min(1, Math.sqrt(maxPixels / (width * height)));
    const scale = Math.min(edgeScale, pixelScale);
    return {
      width: Math.max(1, Math.round(width * scale)),
      height: Math.max(1, Math.round(height * scale)),
    };
  }

  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result || "")), { once: true });
      reader.addEventListener("error", () => reject(new Error("The selected image could not be read.")), { once: true });
      reader.readAsDataURL(file);
    });
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.addEventListener("load", () => resolve(image), { once: true });
      image.addEventListener("error", () => reject(new Error("The image could not be decoded.")), { once: true });
      image.src = url;
    });
  }

  function drawSource() {
    sourceContext.clearRect(0, 0, sourceCanvas.width, sourceCanvas.height);
    if (!state.image) return;
    sourceContext.drawImage(state.image, 0, 0, sourceCanvas.width, sourceCanvas.height);
    if (!state.bbox) return;
    const { x, y, w, h } = state.bbox;
    sourceContext.save();
    sourceContext.fillStyle = "rgba(255, 79, 106, 0.10)";
    sourceContext.strokeStyle = "#ff4f6a";
    sourceContext.lineWidth = Math.max(3, sourceCanvas.width / 420);
    sourceContext.shadowColor = "rgba(255, 79, 106, 0.7)";
    sourceContext.shadowBlur = Math.max(6, sourceCanvas.width / 180);
    sourceContext.fillRect(x, y, w, h);
    sourceContext.strokeRect(x, y, w, h);
    sourceContext.restore();
  }

  function resetResult() {
    state.hasResult = false;
    resultCanvas.width = sourceCanvas.width;
    resultCanvas.height = sourceCanvas.height;
    resultContext.clearRect(0, 0, resultCanvas.width, resultCanvas.height);
    $("repair-result-placeholder").classList.remove("hidden");
    $("repair-result-label").textContent = "Waiting for an edit";
    updateRunState();
  }

  async function loadLocalImage(file) {
    if (!file) return;
    const supported = new Set(["image/jpeg", "image/png", "image/webp"]);
    if (!supported.has(file.type)) {
      setStatus("Use a JPEG, PNG, or WebP image.", "error");
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      setStatus("Choose an image smaller than 20 MB for the browser demo.", "error");
      return;
    }
    try {
      setStatus("Loading the image locally…");
      const dataUrl = await readFileAsDataUrl(file);
      const image = await loadImage(dataUrl);
      const dimensions = fitDimensions(image.naturalWidth, image.naturalHeight);
      sourceCanvas.width = dimensions.width;
      sourceCanvas.height = dimensions.height;
      state.image = image;
      state.filename = file.name;
      state.bbox = null;
      sourceCanvas.classList.add("has-image");
      $("repair-source-placeholder").classList.add("hidden");
      $("repair-selection-label").textContent = "Drag a box around the entity";
      drawSource();
      resetResult();
      setStatus("Image ready. Draw a box around the entity to remove or replace.");
      updateRunState();
    } catch (error) {
      setStatus(error.message || "The selected image could not be loaded.", "error");
    } finally {
      $("repair-image-input").value = "";
    }
  }

  function canvasPoint(event) {
    const rect = sourceCanvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(sourceCanvas.width, (event.clientX - rect.left) * sourceCanvas.width / rect.width)),
      y: Math.max(0, Math.min(sourceCanvas.height, (event.clientY - rect.top) * sourceCanvas.height / rect.height)),
    };
  }

  function updateBoxFromPointer(event) {
    if (!state.dragStart) return;
    const point = canvasPoint(event);
    state.bbox = {
      x: Math.min(state.dragStart.x, point.x),
      y: Math.min(state.dragStart.y, point.y),
      w: Math.abs(point.x - state.dragStart.x),
      h: Math.abs(point.y - state.dragStart.y),
    };
    drawSource();
  }

  function finishBox(event) {
    if (!state.dragStart) return;
    updateBoxFromPointer(event);
    state.dragStart = null;
    if (!state.bbox || state.bbox.w < 8 || state.bbox.h < 8) {
      state.bbox = null;
      drawSource();
      $("repair-selection-label").textContent = "Selection was too small — draw again";
      setStatus("Draw a larger box around the entity.", "error");
    } else {
      const area = Math.round(state.bbox.w * state.bbox.h);
      $("repair-selection-label").textContent = `Region selected · ${area.toLocaleString()} px`;
      setStatus(state.apiKey ? "Region ready. Add entity details and run the edit." : "Region ready. Connect Gemini to run the edit.");
    }
    updateRunState();
  }

  function cropCanvases() {
    const box = state.bbox;
    const padX = Math.max(28, box.w * 0.48);
    const padY = Math.max(28, box.h * 0.48);
    const x = Math.max(0, Math.floor(box.x - padX));
    const y = Math.max(0, Math.floor(box.y - padY));
    const right = Math.min(sourceCanvas.width, Math.ceil(box.x + box.w + padX));
    const bottom = Math.min(sourceCanvas.height, Math.ceil(box.y + box.h + padY));
    const crop = { x, y, w: right - x, h: bottom - y };
    const clean = document.createElement("canvas");
    clean.width = crop.w;
    clean.height = crop.h;
    clean.getContext("2d").drawImage(state.image, -crop.x, -crop.y, sourceCanvas.width, sourceCanvas.height);
    const marked = document.createElement("canvas");
    marked.width = crop.w;
    marked.height = crop.h;
    const context = marked.getContext("2d");
    context.drawImage(clean, 0, 0);
    context.strokeStyle = "#ff2f72";
    context.lineWidth = Math.max(4, Math.min(crop.w, crop.h) * 0.012);
    context.strokeRect(box.x - crop.x, box.y - crop.y, box.w, box.h);
    return { clean, marked, crop };
  }

  function dataUrlPayload(canvas) {
    return canvas.toDataURL("image/png").split(",", 2)[1];
  }

  function repairPrompt(sourceEntity, targetEntity, extraInstruction) {
    const operation = state.mode === "swap"
      ? `Replace only the ${sourceEntity} inside the marked region with ${targetEntity}.`
      : `Remove only the ${sourceEntity} inside the marked region and reconstruct the background naturally.`;
    return [
      "Perform a localized, photorealistic image edit.",
      "The first image is the clean source crop. The second is a location reference; its magenta rectangle marks the only region that may change.",
      operation,
      "Preserve all content outside that region, including composition, identity, lighting, texture, perspective, scale, and shadows.",
      "Do not include the magenta rectangle, labels, borders, explanations, or extra objects in the result.",
      "Return only one edited image with the same dimensions and aspect ratio as the clean source crop.",
      extraInstruction ? `Additional instruction: ${extraInstruction}` : "",
    ].filter(Boolean).join("\n");
  }

  async function parseApiResponse(response) {
    const text = await response.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      const message = payload?.error?.message || text || `Gemini request failed with status ${response.status}.`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function imageFromInteraction(payload) {
    const direct = payload?.output_image;
    if (direct?.data) return { data: direct.data, mimeType: direct.mime_type || direct.mimeType || "image/jpeg" };
    const steps = Array.isArray(payload?.steps) ? payload.steps : [];
    for (let stepIndex = steps.length - 1; stepIndex >= 0; stepIndex -= 1) {
      const step = steps[stepIndex];
      if (step?.type !== "model_output") continue;
      const content = Array.isArray(step.content) ? step.content : [];
      for (let index = content.length - 1; index >= 0; index -= 1) {
        const block = content[index];
        if (block?.type === "image" && block.data) {
          return { data: block.data, mimeType: block.mime_type || block.mimeType || "image/jpeg" };
        }
      }
    }
    return null;
  }

  function imageFromGenerateContent(payload) {
    const candidates = Array.isArray(payload?.candidates) ? payload.candidates : [];
    for (const candidate of candidates) {
      const parts = Array.isArray(candidate?.content?.parts) ? candidate.content.parts : [];
      for (let index = parts.length - 1; index >= 0; index -= 1) {
        const inline = parts[index]?.inlineData || parts[index]?.inline_data;
        if (inline?.data && String(inline.mimeType || inline.mime_type || "").startsWith("image/")) {
          return { data: inline.data, mimeType: inline.mimeType || inline.mime_type };
        }
      }
    }
    return null;
  }

  function findEncodedImage(value) {
    if (!value || typeof value !== "object") return null;
    const mimeType = value.mime_type || value.mimeType || "";
    if (typeof value.data === "string" && String(mimeType).startsWith("image/")) {
      return { data: value.data, mimeType };
    }
    const entries = Array.isArray(value) ? value : Object.values(value);
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const found = findEncodedImage(entries[index]);
      if (found) return found;
    }
    return null;
  }

  async function requestGemini(prompt, cleanData, markedData) {
    const model = $("repair-model").value.trim() || "gemini-3.1-flash-image";
    const interactionResponse = await fetch("https://generativelanguage.googleapis.com/v1beta/interactions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": state.apiKey,
      },
      body: JSON.stringify({
        model,
        input: [
          { type: "text", text: prompt },
          { type: "image", mime_type: "image/png", data: cleanData },
          { type: "image", mime_type: "image/png", data: markedData },
        ],
        response_format: { type: "image", mime_type: "image/jpeg" },
      }),
    });
    const payload = await parseApiResponse(interactionResponse);
    const output = imageFromInteraction(payload) || imageFromGenerateContent(payload) || findEncodedImage(payload);
    if (!output) throw new Error("Gemini returned no editable image. Try a tighter box or a clearer entity description.");
    return output;
  }

  async function compositePatch(output, crop) {
    const patch = await loadImage(`data:${output.mimeType || "image/jpeg"};base64,${output.data}`);
    resultCanvas.width = sourceCanvas.width;
    resultCanvas.height = sourceCanvas.height;
    resultContext.clearRect(0, 0, resultCanvas.width, resultCanvas.height);
    resultContext.drawImage(state.image, 0, 0, resultCanvas.width, resultCanvas.height);

    const layer = document.createElement("canvas");
    layer.width = crop.w;
    layer.height = crop.h;
    const context = layer.getContext("2d", { willReadFrequently: true });
    context.drawImage(patch, 0, 0, crop.w, crop.h);
    const pixels = context.getImageData(0, 0, crop.w, crop.h);
    const feather = Math.max(8, Math.min(52, Math.round(Math.min(crop.w, crop.h) * 0.055)));
    for (let y = 0; y < crop.h; y += 1) {
      for (let x = 0; x < crop.w; x += 1) {
        const edge = Math.min(x, y, crop.w - 1 - x, crop.h - 1 - y);
        const linear = Math.max(0, Math.min(1, edge / feather));
        const smooth = linear * linear * (3 - 2 * linear);
        const alphaIndex = (y * crop.w + x) * 4 + 3;
        pixels.data[alphaIndex] = Math.round(pixels.data[alphaIndex] * smooth);
      }
    }
    context.putImageData(pixels, 0, 0);
    resultContext.drawImage(layer, crop.x, crop.y);
  }

  async function runRepair() {
    if (state.busy) return;
    if (!state.image || !state.bbox) {
      setStatus("Choose an image and draw a repair box first.", "error");
      return;
    }
    if (!state.apiKey) {
      dialog.showModal();
      return;
    }
    const sourceEntity = $("repair-source-entity").value.trim();
    const targetEntity = $("repair-target-entity").value.trim();
    if (!sourceEntity) {
      setStatus("Describe the source entity inside the selected region.", "error");
      $("repair-source-entity").focus();
      return;
    }
    if (state.mode === "swap" && !targetEntity) {
      setStatus("Describe the target entity for the swap.", "error");
      $("repair-target-entity").focus();
      return;
    }

    state.busy = true;
    updateRunState();
    $("repair-run-button").classList.add("loading");
    setStatus(`Running localized ${state.mode === "swap" ? "swap" : "removal"} with Gemini…`);
    try {
      const { clean, marked, crop } = cropCanvases();
      const prompt = repairPrompt(sourceEntity, targetEntity, $("repair-extra-instruction").value.trim());
      const output = await requestGemini(prompt, dataUrlPayload(clean), dataUrlPayload(marked));
      await compositePatch(output, crop);
      state.hasResult = true;
      $("repair-result-placeholder").classList.add("hidden");
      $("repair-result-label").textContent = state.mode === "swap" ? "Localized swap complete" : "Localized removal complete";
      setStatus("Edit complete. Review the result and download it if it is valid.", "success");
    } catch (error) {
      const hint = error instanceof TypeError
        ? "The browser could not reach Gemini. Check the network, browser privacy settings, and API key restrictions."
        : error.message;
      setStatus(hint || "The edit failed. Please try again.", "error");
    } finally {
      state.busy = false;
      $("repair-run-button").classList.remove("loading");
      updateRunState();
    }
  }

  function downloadResult() {
    if (!state.hasResult) return;
    resultCanvas.toBlob((blob) => {
      if (!blob) return;
      const link = document.createElement("a");
      const base = state.filename.replace(/\.[^.]+$/, "") || "sabre-repair";
      link.href = URL.createObjectURL(blob);
      link.download = `${base}__${state.mode === "swap" ? "swapped" : "removed"}.png`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }, "image/png");
  }

  $("repair-image-input").addEventListener("change", (event) => loadLocalImage(event.target.files?.[0]));
  document.querySelectorAll(".repair-mode-switch button").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  document.querySelectorAll("[data-repair-mode]").forEach((link) => {
    link.addEventListener("click", () => setMode(link.dataset.repairMode));
  });

  sourceCanvas.addEventListener("pointerdown", (event) => {
    if (!state.image || state.busy) return;
    state.dragStart = canvasPoint(event);
    state.bbox = null;
    drawSource();
    resetResult();
    sourceCanvas.setPointerCapture(event.pointerId);
  });
  sourceCanvas.addEventListener("pointermove", updateBoxFromPointer);
  sourceCanvas.addEventListener("pointerup", finishBox);
  sourceCanvas.addEventListener("pointercancel", () => {
    state.dragStart = null;
    drawSource();
  });

  $("repair-clear-box").addEventListener("click", () => {
    state.bbox = null;
    drawSource();
    resetResult();
    $("repair-selection-label").textContent = "Drag a box around the entity";
    setStatus("Draw a new box around the entity.");
  });
  $("repair-run-button").addEventListener("click", runRepair);
  $("repair-download").addEventListener("click", downloadResult);

  $("gemini-connect-button").addEventListener("click", () => {
    $("gemini-key-input").value = "";
    $("gemini-key-input").type = "password";
    $("gemini-key-visibility").textContent = "Show";
    $("gemini-key-visibility").setAttribute("aria-pressed", "false");
    updateConnectionState();
    dialog.showModal();
    setTimeout(() => $("gemini-key-input").focus(), 0);
  });
  $("gemini-key-visibility").addEventListener("click", () => {
    const input = $("gemini-key-input");
    const visible = input.type === "text";
    input.type = visible ? "password" : "text";
    $("gemini-key-visibility").textContent = visible ? "Show" : "Hide";
    $("gemini-key-visibility").setAttribute("aria-pressed", String(!visible));
  });
  $("gemini-use-button").addEventListener("click", () => {
    const value = $("gemini-key-input").value.trim();
    if (!value) {
      $("gemini-key-input").focus();
      return;
    }
    state.apiKey = value;
    $("gemini-key-input").value = "";
    updateConnectionState();
    dialog.close();
    setStatus(state.image && state.bbox ? "Gemini connected. Ready to run the edit." : "Gemini connected. Choose an image and mark the edit region.");
  });
  $("gemini-disconnect-button").addEventListener("click", () => {
    state.apiKey = "";
    $("gemini-key-input").value = "";
    updateConnectionState();
    dialog.close();
    setStatus("Gemini key cleared from this tab.");
  });

  window.addEventListener("pagehide", () => {
    state.apiKey = "";
    $("gemini-key-input").value = "";
  });

  setMode("remove");
  updateConnectionState();
  updateRunState();
})();
