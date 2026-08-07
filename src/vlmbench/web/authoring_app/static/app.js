let items = [];
let decisions = {};
let filtered = [];
let cursor = 0;
let currentBoxes = { base: null, edited: null };
let activeMode = "review";
let uploadRepair = null;
let rejectReasonOpen = false;
let dirtyQuestionItems = new Set();
let authoring = { items: [], cursor: 0, box: null, candidate: null, draft: null, editHistory: [], roots: {} };

const $ = (id) => document.getElementById(id);
const statusButtons = [...document.querySelectorAll("[data-status]")];
const THEME_STORAGE_KEY = "vlmbench-review-theme";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme, persist = false) {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  const button = $("theme-toggle");
  button.textContent = `Theme: ${normalized === "light" ? "Light" : "Dark"}`;
  button.setAttribute("aria-pressed", String(normalized === "light"));
  button.setAttribute(
    "aria-label",
    normalized === "light" ? "Switch to dark theme" : "Switch to light theme"
  );
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch (_error) {
      // The theme still applies for this page when storage is unavailable.
    }
  }
}

function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark", true);
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function isRejected(status) {
  return status && status.startsWith("reject_");
}

function normalizeDecisionStatus(status) {
  if (status === "needs_recheck") return "needs_repair";
  if (status === "reject_scene_changed") return "reject_edit_failed";
  return status || "";
}

function rejectionReason(status) {
  if (status === "reject_edit_failed") return "edit_failed";
  if (status === "reject_base_invalid") return "base_invalid";
  return "";
}

function decisionLabel(status) {
  return {
    keep: "keep",
    needs_repair: "needs repair",
    reject_edit_failed: "reject · edit failed",
    reject_base_invalid: "reject · base invalid",
    unsure: "unsure"
  }[normalizeDecisionStatus(status)] || "unreviewed";
}

function renderDecisionControls(status) {
  const normalized = normalizeDecisionStatus(status);
  statusButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.status === normalized);
  });
  const rejected = isRejected(normalized);
  const expanded = rejected || rejectReasonOpen;
  $("reject-toggle").classList.toggle("active", rejected);
  $("reject-toggle").setAttribute("aria-expanded", String(expanded));
  $("reject-reasons").hidden = !expanded;
}

function withCacheBuster(url, stamp) {
  if (!stamp) return url;
  return `${url}?v=${encodeURIComponent(stamp)}`;
}

function applyFilter() {
  rejectReasonOpen = false;
  const value = $("filter").value;
  filtered = items.filter((item) => {
    const decision = decisions[item.review_id];
    const status = normalizeDecisionStatus(decision?.status);
    const repairStatus = decision?.repair_status === "needs_recheck"
      ? "needs_repair"
      : decision?.repair_status;
    if (value === "all") return true;
    if (value === "unreviewed") return !status;
    if (value === "keep") return status === "keep";
    if (value === "needs_repair") return status === "needs_repair" || repairStatus === "needs_repair";
    if (value === "reject") return isRejected(status);
    return item.priority === value;
  });
  cursor = Math.min(cursor, Math.max(0, filtered.length - 1));
  render();
}

function boxStyle(kind) {
  const box = currentBoxes[kind];
  const elements = kind === "edited" ? [$("edited-box"), $("repair-edited-box")] : [$(`${kind}-box`)];
  elements.forEach((element) => {
    if (!element) return;
    if (!box) {
      element.style.display = "none";
      return;
    }
    element.style.display = "block";
    element.style.left = `${box.x * 100}%`;
    element.style.top = `${box.y * 100}%`;
    element.style.width = `${box.w * 100}%`;
    element.style.height = `${box.h * 100}%`;
  });
}

function setPreviewImage(url) {
  const stage = $("repair-preview-image").closest(".preview-stage");
  if (!url) {
    $("repair-preview-image").removeAttribute("src");
    stage.classList.remove("has-preview");
    return;
  }
  $("repair-preview-image").src = url;
  stage.classList.add("has-preview");
}

function setText(id, value) {
  $(id).textContent = value || "—";
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function updateRepairFields() {
  const isSwap = $("repair-type").value === "swap_entity";
  $("swap-fields").style.display = isSwap ? "grid" : "none";
  $("repair-entity").closest("label").style.display = isSwap ? "none" : "block";
  $("repair-prompt").placeholder = isSwap
    ? "Example: Replace the green tape roll inside the marked region with a red apple. Match lighting, perspective, scale, and shadows. Keep everything else unchanged."
    : "Example: Remove the remaining PTFE tape roll inside the marked region. Fill with matching workbench clutter/background. Do not change the dental floss dispenser.";
}

function normalizeRepairType(value) {
  if (value === "remove_residual_source" || value === "remove_extra_target") return "remove_entity";
  return value || "remove_entity";
}

function updateMode(mode) {
  activeMode = mode;
  document.body.dataset.mode = mode;
  document.querySelectorAll("[data-view]").forEach((section) => {
    const view = section.dataset.view;
    const views = view.split(/\s+/);
    section.classList.toggle("mode-hidden", !views.includes("all") && !views.includes(mode));
  });
  document.querySelectorAll(".mode-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  if (mode === "author") renderAuthoring();
  else render();
}

function render() {
  if (activeMode === "author") {
    renderAuthoring();
    return;
  }
  if (!filtered.length) {
    $("summary").textContent = "No items match the current filter.";
    return;
  }
  const item = filtered[cursor];
  const decision = decisions[item.review_id] || {};
  const reviewed = Object.values(decisions).filter((x) => x.status).length;
  $("summary").textContent = `Current ${cursor + 1}/${filtered.length} · Reviewed ${reviewed}/${items.length}`;
  $("jump").value = cursor + 1;
  setText("item-title", `${item.review_id} · ${item.run} · ${item.pair_id}`);
  setText("priority", item.priority);
  setText("source-entity", item.source_entity);
  setText("target-entity", item.inserted_entity);
  setText("review-location", item.review_location);
  setText("cluster", item.source_cluster_description);
  setText("target-strategy", item.target_visual_strategy);
  setText("verifiability", item.human_verifiability);
  $("base-image").src = item.base_url;
  const editedUrl = decision.repaired_edited_image ? `/${decision.repaired_edited_image}` : item.edited_url;
  $("edited-image").src = withCacheBuster(editedUrl, decision.updated_at || "");
  $("edited-source-state").textContent = decision.repaired_edited_image
    ? `Showing repaired image: ${decision.repaired_edited_image}`
    : "Showing original edited image.";
  if (uploadRepair) {
    $("repair-current-image").src = withCacheBuster(uploadRepair.source_url, uploadRepair.updated_at || "");
    $("repair-current-state").textContent = `Uploaded image: ${uploadRepair.source_image}`;
    if (uploadRepair.repaired_url) {
      setPreviewImage(withCacheBuster(uploadRepair.repaired_url, uploadRepair.updated_at || ""));
      $("repair-preview-state").textContent = `Uploaded repair result: ${uploadRepair.repaired_image}`;
    } else {
      setPreviewImage("");
      $("repair-preview-state").textContent = "Run patch repair to create a candidate.";
    }
    $("repair-accept").disabled = true;
    $("repair-reset").textContent = "Back to benchmark image";
  } else {
    $("repair-current-image").src = withCacheBuster(item.edited_url, decision.updated_at || "");
    $("repair-current-state").textContent = "Repair input: current edited image.";
    if (decision.repaired_edited_image) {
      setPreviewImage(withCacheBuster(`/${decision.repaired_edited_image}`, decision.updated_at || ""));
      $("repair-preview-state").textContent = `Preview: ${decision.repaired_edited_image}`;
    } else {
      setPreviewImage("");
      $("repair-preview-state").textContent = "Run patch repair to create a candidate.";
    }
    $("repair-accept").disabled = !decision.repaired_edited_image;
    $("repair-reset").textContent = "Use original edited image";
  }
  currentBoxes = {
    base: decision.base_bbox || null,
    edited: uploadRepair ? uploadRepair.bbox || null : decision.edited_bbox || null
  };
  boxStyle("base"); boxStyle("edited");
  const repairForm = uploadRepair || {};
  $("notes").value = decision.notes || "";
  $("repair-entity").value = uploadRepair ? repairForm.repair_entity || "" : decision.repair_entity || "";
  $("repair-source-entity").value = uploadRepair ? repairForm.repair_source_entity || "" : decision.repair_source_entity || "";
  $("repair-target-entity").value = uploadRepair ? repairForm.repair_target_entity || "" : decision.repair_target_entity || "";
  $("repair-type").value = normalizeRepairType(uploadRepair ? repairForm.repair_type : decision.repair_type);
  updateRepairFields();
  $("repair-prompt").value = uploadRepair ? repairForm.repair_prompt || "" : decision.repair_prompt || "";
  $("repair-state").textContent = uploadRepair
    ? uploadRepair.repaired_image
      ? `Uploaded repair saved: ${uploadRepair.repaired_image}`
      : "Upload repair mode."
    : decision.repair_status
      ? `Repair status: ${decision.repair_status}${
          decision.repaired_edited_image ? `\nCurrent repaired image: ${decision.repaired_edited_image}` : ""
        }`
      : "";
  renderDecisionControls(decision.status);
  $("save-state").textContent = decision.status ? `Saved: ${decisionLabel(decision.status)}` : "Unreviewed";
  const body = $("probe-body");
  body.innerHTML = "";
  Object.entries(item.probes).forEach(([probe, row]) => {
    const tr = document.createElement("tr");
    tr.dataset.questionId = row.question_id;
    tr.innerHTML = `
      <td>${escapeHtml(probe)}</td>
      <td><textarea class="review-question-input" rows="2">${escapeHtml(row.question)}</textarea></td>
      <td><input class="review-answer-input" value="${escapeHtml(row.expected_answer)}"></td>
      <td>${escapeHtml(row.gemini_prediction)}</td>
      <td class="${row.correct ? "correct" : "wrong"}">${row.correct ? "Correct" : "Incorrect"}</td>
    `;
    tr.querySelector(".review-question-input").addEventListener("input", (event) => {
      row.question = event.target.value;
      dirtyQuestionItems.add(item.review_id);
      $("question-save-state").textContent = "Unsaved question changes.";
    });
    tr.querySelector(".review-answer-input").addEventListener("input", (event) => {
      row.expected_answer = event.target.value;
      dirtyQuestionItems.add(item.review_id);
      $("question-save-state").textContent = "Unsaved question changes.";
    });
    body.appendChild(tr);
  });
  const questionsDisabled = Boolean(uploadRepair);
  $("save-questions").disabled = questionsDisabled;
  body.querySelectorAll("input,textarea").forEach((field) => { field.disabled = questionsDisabled; });
  $("question-save-state").textContent = questionsDisabled
    ? "Uploaded images have no benchmark question JSON."
    : dirtyQuestionItems.has(item.review_id)
      ? "Unsaved question changes."
      : "";
}

async function saveQuestions() {
  if (!filtered.length || uploadRepair) return false;
  const item = filtered[cursor];
  const questions = [...document.querySelectorAll("#probe-body tr")].map((tr) => ({
    question_id: tr.dataset.questionId,
    prompt: tr.querySelector(".review-question-input").value.trim(),
    answer: tr.querySelector(".review-answer-input").value.trim()
  }));
  $("question-save-state").textContent = "Saving...";
  try {
    const result = await api("/api/questions", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ review_id:item.review_id, questions })
    });
    item.probes = result.probes;
    dirtyQuestionItems.delete(item.review_id);
    render();
    $("question-save-state").textContent = result.changed_question_ids.length
      ? `Saved ${result.changed_question_ids.length} change(s) to questions.jsonl.`
      : "No changes to save.";
    return true;
  } catch (error) {
    $("question-save-state").textContent = `Save failed: ${error.message}`;
    return false;
  }
}

async function save(status, advance = false) {
  if (!filtered.length) return;
  const item = filtered[cursor];
  if (dirtyQuestionItems.has(item.review_id) && !(await saveQuestions())) return;
  const old = decisions[item.review_id] || {};
  const resolvedStatus = normalizeDecisionStatus(status || old.status || "");
  const payload = {
    ...old,
    review_id: item.review_id,
    run: item.run,
    pair_id: item.pair_id,
    priority: item.priority,
    status: resolvedStatus,
    reason: rejectionReason(resolvedStatus),
    base_bbox: currentBoxes.base,
    edited_bbox: currentBoxes.edited,
    repair_type: $("repair-type").value,
    repair_entity: $("repair-entity").value.trim(),
    repair_source_entity: $("repair-source-entity").value.trim(),
    repair_target_entity: $("repair-target-entity").value.trim(),
    repair_prompt: $("repair-prompt").value.trim(),
    notes: $("notes").value.trim()
  };
  $("save-state").textContent = "Saving...";
  await api("/api/decision", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
  decisions[item.review_id] = payload;
  $("save-state").textContent = `Saved: ${payload.status || "boxes only"}`;
  if (advance) move(1);
}

async function exportBoxedImages() {
  if (!filtered.length || uploadRepair) return;
  const item = filtered[cursor];
  const state = $("boxed-export-state");
  if (!currentBoxes.base && !currentBoxes.edited) {
    state.textContent = "Draw at least one Base or Edited box first.";
    return;
  }
  const button = $("export-boxed-images");
  button.disabled = true;
  state.textContent = "Exporting...";
  try {
    const result = await api("/api/export-boxed-images", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        review_id:item.review_id,
        base_bbox:currentBoxes.base,
        edited_bbox:currentBoxes.edited
      })
    });
    state.textContent = `Saved: ${result.exports.map((row) => row.boxed_image).join(" · ")}`;
  } catch (error) {
    state.textContent = `Export failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function repairEdited() {
  if (!filtered.length) return;
  const item = filtered[cursor];
  if (!uploadRepair && dirtyQuestionItems.has(item.review_id) && !(await saveQuestions())) return;
  if (!currentBoxes.edited) {
    $("repair-state").textContent = "Draw an Edited image box before repairing.";
    return;
  }
  if (uploadRepair) {
    const payload = {
      source_image: uploadRepair.source_image,
      bbox: currentBoxes.edited,
      repair_type: $("repair-type").value,
      entity_to_remove: $("repair-entity").value.trim(),
      source_entity: $("repair-source-entity").value.trim(),
      target_entity: $("repair-target-entity").value.trim(),
      prompt: $("repair-prompt").value.trim(),
      model: $("repair-model").value.trim()
    };
    $("repair-state").textContent = "Repairing uploaded image...";
    try {
      const result = await api("/api/upload-repair", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(payload)
      });
      uploadRepair = {
        ...uploadRepair,
        bbox: currentBoxes.edited,
        repair_type: $("repair-type").value,
        repair_entity: $("repair-entity").value.trim(),
        repair_source_entity: $("repair-source-entity").value.trim(),
        repair_target_entity: $("repair-target-entity").value.trim(),
        repair_prompt: $("repair-prompt").value.trim(),
        repaired_image: result.repair.repaired_image,
        repaired_url: result.repair.repaired_url,
        updated_at: new Date().toISOString()
      };
      $("repair-state").textContent = `Uploaded repair saved: ${result.repair.repaired_image}`;
      render();
    } catch (error) {
      $("repair-state").textContent = `Repair failed: ${error.message}`;
    }
    return;
  }
  const payload = {
    review_id: item.review_id,
    bbox: currentBoxes.edited,
    engine: $("repair-engine").value,
    repair_type: $("repair-type").value,
    entity_to_remove: $("repair-entity").value.trim(),
    source_entity: $("repair-source-entity").value.trim(),
    target_entity: $("repair-target-entity").value.trim(),
    prompt: $("repair-prompt").value.trim(),
    model: $("repair-model").value.trim()
  };
  $("repair-state").textContent = "Repairing...";
  try {
    const result = await api("/api/repair", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    });
    decisions[item.review_id] = result.decision;
    $("repair-state").textContent = `Repair saved: ${result.repair.engine}\n${result.repair.repaired_image}`;
    render();
  } catch (error) {
    $("repair-state").textContent = `Repair failed: ${error.message}`;
  }
}

async function resetRepair() {
  if (!filtered.length) return;
  if (uploadRepair) {
    uploadRepair = null;
    $("upload-repair-file").value = "";
    $("upload-state").textContent = "Using current benchmark image.";
    render();
    return;
  }
  const item = filtered[cursor];
  $("repair-state").textContent = "Resetting repaired image...";
  try {
    const result = await api("/api/reset-repair", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ review_id:item.review_id })
    });
    decisions[item.review_id] = result.decision;
    $("repair-state").textContent = "Showing original edited image.";
    render();
  } catch (error) {
    $("repair-state").textContent = `Reset failed: ${error.message}`;
  }
}

async function acceptRepair() {
  if (!filtered.length) return;
  if (uploadRepair) {
    $("repair-state").textContent = "Uploaded repairs are saved as preview files; accept is only for benchmark images.";
    return;
  }
  const item = filtered[cursor];
  if (dirtyQuestionItems.has(item.review_id) && !(await saveQuestions())) return;
  if (!decisions[item.review_id]?.repaired_edited_image) {
    $("repair-state").textContent = "No repaired image to accept.";
    return;
  }
  $("repair-state").textContent = "Accepting repaired image...";
  try {
    const result = await api("/api/accept-repair", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ review_id:item.review_id })
    });
    decisions[item.review_id] = result.decision;
    $("repair-state").textContent = `Accepted repaired image into edited image.\nBackup: ${result.backup_previous_edited_image}`;
    render();
  } catch (error) {
    $("repair-state").textContent = `Accept failed: ${error.message}`;
  }
}

async function uploadRepairImage(file) {
  if (!file) return;
  $("upload-state").textContent = "Uploading image...";
  try {
    const dataUrl = await fileToDataUrl(file);
    const result = await api("/api/upload-image", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ filename:file.name, data_url:dataUrl })
    });
    uploadRepair = {
      source_image: result.source_image,
      source_url: result.source_url,
      bbox: null,
      repaired_image: "",
      repaired_url: "",
      updated_at: new Date().toISOString()
    };
    currentBoxes.edited = null;
    $("upload-state").textContent = `Uploaded: ${result.source_image}`;
    updateMode("repair");
    render();
  } catch (error) {
    $("upload-state").textContent = `Upload failed: ${error.message}`;
  }
}

async function loadAuthoringItems() {
  const result = await api("/api/authoring/items");
  authoring.items = result.items || [];
  authoring.roots = result;
  authoring.cursor = Math.min(authoring.cursor, Math.max(0, authoring.items.length - 1));
}

async function uploadAuthoringImages(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  const trigger = $("author-upload-trigger");
  trigger.disabled = true;
  const uploaded = [];
  const failed = [];
  try {
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      $("authoring-state").textContent = `Uploading ${index + 1}/${files.length}: ${file.name}`;
      try {
        const dataUrl = await fileToDataUrl(file);
        const result = await api("/api/authoring/upload", {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({ filename:file.name, data_url:dataUrl })
        });
        uploaded.push(result.upload);
      } catch (error) {
        failed.push({ name:file.name, error:error.message });
      }
    }
    if (uploaded.length) {
      await loadAuthoringItems();
      const first = uploaded[0];
      const index = authoring.items.findIndex((row) =>
        (first.item_id && row.id === first.item_id)
        || row.original_filename === first.original_filename
      );
      if (index >= 0) authoring.cursor = index;
      authoring.box = null;
      authoring.candidate = null;
      authoring.draft = null;
      authoring.editHistory = [];
      renderAuthoring();
    }
    if (failed.length) {
      const names = failed.map((row) => row.name).join(", ");
      $("authoring-state").textContent = `Uploaded ${uploaded.length}/${files.length}. Failed: ${names}`;
    } else {
      $("authoring-state").textContent = `Uploaded ${uploaded.length} image${uploaded.length === 1 ? "" : "s"}. Ready to author.`;
    }
  } finally {
    trigger.disabled = false;
    $("author-upload-files").value = "";
  }
}

function currentAuthorItem() {
  return authoring.items[authoring.cursor] || null;
}

function setAuthorPreview(url) {
  const stage = $("author-preview-image").closest(".preview-stage");
  if (!url) {
    $("author-preview-image").removeAttribute("src");
    stage.classList.remove("has-preview");
    return;
  }
  $("author-preview-image").src = url;
  stage.classList.add("has-preview");
}

function authorBoxStyle() {
  const box = authoring.box;
  const element = $("author-base-box");
  if (!box) {
    element.style.display = "none";
    return;
  }
  element.style.display = "block";
  element.style.left = `${box.x * 100}%`;
  element.style.top = `${box.y * 100}%`;
  element.style.width = `${box.w * 100}%`;
  element.style.height = `${box.h * 100}%`;
}

function authorDefaultQuestions(record = null) {
  const item = record || collectAuthorForm(false);
  const id = item.id || "real_001";
  const source = item.source_entity || "source object";
  const target = item.target_entity || "target object";
  const scene = item.scene_description || "scene";
  return [
    { probe:"base_source", image_role:"base", question:`Is there a ${source} visible in this ${scene}?`, answer:"yes" },
    { probe:"base_target", image_role:"base", question:`Is there a ${target} visible in this ${scene}?`, answer:"no" },
    { probe:"edited_source", image_role:"edited", question:`Is there a ${source} visible in this ${scene}?`, answer:"no" },
    { probe:"edited_target", image_role:"edited", question:`Is there a ${target} visible in this ${scene}?`, answer:"yes" },
  ].map((row) => ({ ...row, id:`${id}__${row.probe}`, pair_id:id }));
}

function renderAuthorQuestions(questions) {
  const body = $("author-question-body");
  body.innerHTML = "";
  (questions || []).forEach((row) => {
    const questionType = row.question_type || "yes_no";
    const evalType = row.eval_type || (questionType === "yes_no" ? "yes_no_exact" : questionType === "multiple_choice" ? "choice_exact" : "manual");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input class="probe-input" value="${escapeHtml(row.probe || "")}"></td>
      <td>
        <select class="image-role-input">
          <option value="base"${row.image_role === "base" ? " selected" : ""}>base</option>
          <option value="edited"${row.image_role === "edited" ? " selected" : ""}>edited</option>
        </select>
      </td>
      <td>
        <select class="question-type-input">
          <option value="yes_no"${questionType === "yes_no" ? " selected" : ""}>yes/no</option>
          <option value="multiple_choice"${questionType === "multiple_choice" ? " selected" : ""}>multi-choice</option>
          <option value="open_generation"${questionType === "open_generation" ? " selected" : ""}>open-gen</option>
          <option value="short_answer"${questionType === "short_answer" ? " selected" : ""}>short</option>
        </select>
      </td>
      <td><input class="question-input" value="${escapeHtml(row.question || "")}"></td>
      <td><input class="answer-input" value="${escapeHtml(row.answer || "")}" placeholder="yes / A / free text"></td>
      <td>
        <select class="eval-type-input">
          <option value="yes_no_exact"${evalType === "yes_no_exact" ? " selected" : ""}>yes/no exact</option>
          <option value="choice_exact"${evalType === "choice_exact" ? " selected" : ""}>choice exact</option>
          <option value="contains"${evalType === "contains" ? " selected" : ""}>contains</option>
          <option value="manual"${evalType === "manual" ? " selected" : ""}>manual</option>
        </select>
      </td>
      <td><button class="author-remove-question" type="button">x</button></td>
    `;
    const typeInput = tr.querySelector(".question-type-input");
    const evalInput = tr.querySelector(".eval-type-input");
    typeInput.addEventListener("change", () => {
      if (typeInput.value === "yes_no") evalInput.value = "yes_no_exact";
      else if (typeInput.value === "multiple_choice") evalInput.value = "choice_exact";
      else evalInput.value = "manual";
    });
    tr.querySelector(".author-remove-question").addEventListener("click", () => tr.remove());
    body.appendChild(tr);
  });
}

function collectAuthorQuestions() {
  return [...document.querySelectorAll("#author-question-body tr")].map((tr) => ({
    probe: tr.querySelector(".probe-input").value.trim(),
    image_role: tr.querySelector(".image-role-input").value,
    question_type: tr.querySelector(".question-type-input").value,
    question: tr.querySelector(".question-input").value.trim(),
    answer: tr.querySelector(".answer-input").value.trim(),
    eval_type: tr.querySelector(".eval-type-input").value,
  }));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function collectAuthorForm(includeQuestions = true) {
  return {
    id: $("author-id").value.trim(),
    edit_type: $("author-edit-type").value,
    source_entity: $("author-source").value.trim(),
    target_entity: $("author-target").value.trim(),
    scene_description: $("author-scene").value.trim(),
    review_location: $("author-location").value.trim(),
    prompt: $("author-prompt").value.trim(),
    model: $("author-model").value.trim(),
    bbox: authoring.box,
    questions: includeQuestions ? collectAuthorQuestions() : [],
  };
}

function renderAuthoring() {
  const item = currentAuthorItem();
  $("summary").textContent = authoring.items.length
    ? `Authoring ${authoring.cursor + 1}/${authoring.items.length}`
    : "No authoring images found.";
  $("authoring-roots").textContent = `Input: ${authoring.roots.input_root || "—"} · Output: ${authoring.roots.output_root || "—"}`;
  $("author-jump").value = authoring.items.length ? authoring.cursor + 1 : "";
  if (!item) {
    $("authoring-state").textContent = "No images in the authoring input folder.";
    $("author-base-image").removeAttribute("src");
    setAuthorPreview("");
    renderAuthorQuestions([]);
    return;
  }
  const record = item.record || {};
  const recoveredCandidate = item.latest_candidate || {};
  const draft = authoring.draft?.original_filename === item.original_filename ? authoring.draft : null;
  const recoveredPayload = recoveredCandidate.metadata?.payload || {};
  const recoveredEdit = recoveredCandidate.metadata?.edit || {};
  const recoveredData = {
    ...recoveredPayload,
    ...recoveredEdit,
    questions: recoveredPayload.questions || recoveredEdit.questions || []
  };
  const formData = draft || record || recoveredData;
  const effectiveFormData = Object.keys(record).length || draft ? formData : recoveredData;
  $("author-base-image").src = item.source_url;
  $("author-base-state").textContent = `${item.original_filename}${item.saved ? " · saved" : ""}`;
  $("author-id").value = effectiveFormData.id || item.id || "";
  $("author-edit-type").value = effectiveFormData.edit_type || "swap_entity";
  $("author-source").value = effectiveFormData.source_entity || "";
  $("author-target").value = effectiveFormData.target_entity || "";
  $("author-scene").value = effectiveFormData.scene_description || "";
  $("author-location").value = effectiveFormData.review_location || "";
  $("author-prompt").value = effectiveFormData.prompt || "";
  authoring.box = authoring.candidate?.original_filename === item.original_filename
    ? authoring.box
    : record.source_bbox || recoveredEdit.bbox || recoveredPayload.bbox || null;
  authorBoxStyle();
  const hasFreshCandidate = authoring.candidate?.original_filename === item.original_filename;
  const hasRecoveredCandidate = Boolean(recoveredCandidate.candidate_url);
  const recoveredIsAccepted = recoveredCandidate.candidate_image && record.candidate_image
    ? recoveredCandidate.candidate_image === record.candidate_image
    : false;
  if (hasFreshCandidate) {
    setAuthorPreview(authoring.candidate.candidate_url);
    $("author-preview-state").textContent = `Candidate: ${authoring.candidate.candidate_image}`;
  } else if (hasRecoveredCandidate && !recoveredIsAccepted) {
    setAuthorPreview(recoveredCandidate.candidate_url);
    const matchNote = recoveredCandidate.match_source_id && recoveredCandidate.match_source_id !== item.id
      ? ` · visually matched from ${recoveredCandidate.match_source_id}`
      : "";
    $("author-preview-state").textContent = `New unsaved candidate: ${recoveredCandidate.candidate_image}${matchNote}`;
  } else if (record.edited_url) {
    setAuthorPreview(record.edited_url);
    $("author-preview-state").textContent = `Saved edited image: ${record.edited_image}`;
  } else if (recoveredCandidate.candidate_url) {
    setAuthorPreview(recoveredCandidate.candidate_url);
    const matchNote = recoveredCandidate.match_source_id && recoveredCandidate.match_source_id !== item.id
      ? ` · visually matched from ${recoveredCandidate.match_source_id}`
      : "";
    $("author-preview-state").textContent = `Recovered candidate: ${recoveredCandidate.candidate_image}${matchNote}`;
  } else {
    setAuthorPreview("");
    $("author-preview-state").textContent = "Run edit to create an edited candidate.";
  }
  const questionRows = effectiveFormData.questions?.length
    ? effectiveFormData.questions
    : record.questions?.length
      ? record.questions
      : authorDefaultQuestions(effectiveFormData.id || effectiveFormData.source_entity ? effectiveFormData : null);
  renderAuthorQuestions(questionRows);
  $("authoring-state").textContent = item.saved
    ? `Saved item: ${record.id}`
    : recoveredCandidate.candidate_image
      ? "Unsaved item with recovered candidate."
      : "Unsaved item.";
}

function moveAuthor(delta) {
  if (!authoring.items.length) return;
  authoring.cursor = Math.max(0, Math.min(authoring.items.length - 1, authoring.cursor + delta));
  authoring.box = null;
  authoring.candidate = null;
  authoring.draft = null;
  renderAuthoring();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function runAuthorEdit() {
  const item = currentAuthorItem();
  if (!item) return;
  const form = collectAuthorForm(false);
  if (!form.bbox) {
    $("authoring-state").textContent = "Draw a source entity box before running edit.";
    return;
  }
  $("authoring-state").textContent = "Running edit...";
  try {
    authoring.draft = { ...form, original_filename:item.original_filename };
    const result = await api("/api/authoring/edit", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ ...form, original_filename:item.original_filename, source_image:item.source_image || "" })
    });
    authoring.candidate = { ...result.edit, original_filename:item.original_filename };
    authoring.editHistory = [...(authoring.editHistory || []), result.edit];
    $("authoring-state").textContent = `Candidate ready: ${result.edit.candidate_image}`;
    renderAuthoring();
  } catch (error) {
    $("authoring-state").textContent = `Edit failed: ${error.message}`;
  }
}

async function saveAuthorItem() {
  const item = currentAuthorItem();
  if (!item) return;
  const form = collectAuthorForm(true);
  const recoveredCandidate = item.latest_candidate || {};
  const freshCandidate = authoring.candidate?.original_filename === item.original_filename ? authoring.candidate : {};
  const record = item.record || {};
  const candidateImage = freshCandidate.candidate_image
    || recoveredCandidate.candidate_image
    || record.candidate_image
    || record.edited_image
    || "";
  if (!form.source_entity || !form.scene_description || (form.edit_type === "swap_entity" && !form.target_entity)) {
    $("authoring-state").textContent = "Fill Source entity, Scene description, and Target entity for swap edits before saving.";
    return;
  }
  if (!candidateImage) {
    $("authoring-state").textContent = "No edited candidate found for this item. Run edit first.";
    return;
  }
  $("authoring-state").textContent = "Saving item...";
  try {
    const result = await api("/api/authoring/save", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        ...form,
        original_filename:item.original_filename,
        source_image:item.source_image || "",
        candidate_image:candidateImage,
        edit_history:authoring.editHistory,
        created_at:item.record?.created_at || "",
      })
    });
    await loadAuthoringItems();
    const idx = authoring.items.findIndex((row) => row.original_filename === item.original_filename);
    authoring.cursor = idx >= 0 ? idx : authoring.cursor;
    authoring.candidate = null;
    authoring.draft = null;
    authoring.editHistory = [];
    const exportInfo = result.export || {};
    const exported = exportInfo.generic_question_count ?? exportInfo.question_count ?? 0;
    $("authoring-state").textContent = `Saved ${result.record.id}. Exported ${exported} questions.`;
    renderAuthoring();
  } catch (error) {
    $("authoring-state").textContent = `Save failed: ${error.message}`;
  }
}

async function exportAuthoring() {
  $("authoring-state").textContent = "Exporting JSONL...";
  try {
    const result = await api("/api/authoring/export", { method:"POST", headers:{"Content-Type":"application/json"}, body:"{}" });
    const exportInfo = result.export || {};
    if (exportInfo.generic_question_count !== undefined) {
      $("authoring-state").textContent = `Exported ${exportInfo.generic_pair_count} samples and ${exportInfo.generic_question_count} generic questions.`;
    } else {
      $("authoring-state").textContent = `Exported ${exportInfo.pair_count} pairs and ${exportInfo.question_count} questions.`;
    }
  } catch (error) {
    $("authoring-state").textContent = `Export failed: ${error.message}`;
  }
}

function move(delta) {
  if (!filtered.length) return;
  rejectReasonOpen = false;
  cursor = Math.max(0, Math.min(filtered.length - 1, cursor + delta));
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function enableBox(kind, stageId = `${kind}-stage`) {
  const stage = $(stageId);
  let start = null;
  stage.addEventListener("pointerdown", (event) => {
    if (activeMode === "repair" && kind === "base") return;
    const rect = stage.getBoundingClientRect();
    start = { x:(event.clientX - rect.left) / rect.width, y:(event.clientY - rect.top) / rect.height };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!start) return;
    const rect = stage.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    currentBoxes[kind] = { x:Math.min(start.x,x), y:Math.min(start.y,y), w:Math.abs(x-start.x), h:Math.abs(y-start.y) };
    boxStyle(kind);
  });
  stage.addEventListener("pointerup", async (event) => {
    if (!start) return;
    const rect = stage.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    let box = { x:Math.min(start.x,x), y:Math.min(start.y,y), w:Math.abs(x-start.x), h:Math.abs(y-start.y) };
    if (box.w < 0.015 && box.h < 0.015) {
      box = { x:Math.max(0, start.x - 0.03), y:Math.max(0, start.y - 0.03), w:0.06, h:0.06 };
      box.w = Math.min(box.w, 1 - box.x); box.h = Math.min(box.h, 1 - box.y);
    }
    currentBoxes[kind] = box;
    if (uploadRepair && activeMode === "repair" && kind === "edited") {
      uploadRepair.bbox = box;
      start = null;
      boxStyle(kind);
      return;
    }
    start = null;
    boxStyle(kind);
    await save(null, false);
  });
}

async function init() {
  const [loadedItems, loadedDecisions] = await Promise.all([api("/api/items"), api("/api/decisions")]);
  items = loadedItems;
  decisions = Object.fromEntries(loadedDecisions.map((row) => [row.review_id, row]));
  await loadAuthoringItems();
  enableBox("base");
  enableBox("edited");
  enableBox("edited", "repair-edited-stage");
  enableAuthorBox();
  updateMode(activeMode);
  applyFilter();
}

function enableAuthorBox() {
  const stage = $("author-base-stage");
  let start = null;
  stage.addEventListener("pointerdown", (event) => {
    if (activeMode !== "author") return;
    const rect = stage.getBoundingClientRect();
    start = { x:(event.clientX - rect.left) / rect.width, y:(event.clientY - rect.top) / rect.height };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!start) return;
    const rect = stage.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    authoring.box = { x:Math.min(start.x,x), y:Math.min(start.y,y), w:Math.abs(x-start.x), h:Math.abs(y-start.y) };
    authorBoxStyle();
  });
  stage.addEventListener("pointerup", (event) => {
    if (!start) return;
    const rect = stage.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    let box = { x:Math.min(start.x,x), y:Math.min(start.y,y), w:Math.abs(x-start.x), h:Math.abs(y-start.y) };
    if (box.w < 0.015 && box.h < 0.015) {
      box = { x:Math.max(0, start.x - 0.03), y:Math.max(0, start.y - 0.03), w:0.06, h:0.06 };
      box.w = Math.min(box.w, 1 - box.x); box.h = Math.min(box.h, 1 - box.y);
    }
    authoring.box = box;
    start = null;
    authorBoxStyle();
  });
}

$("filter").addEventListener("change", () => { cursor = 0; applyFilter(); });
$("prev").addEventListener("click", () => move(-1));
$("next").addEventListener("click", () => move(1));
$("jump").addEventListener("change", () => {
  rejectReasonOpen = false;
  cursor = Math.max(0, Math.min(filtered.length - 1, Number($("jump").value) - 1));
  render();
});
$("save").addEventListener("click", () => save(null, false));
$("export-boxed-images").addEventListener("click", exportBoxedImages);
$("save-questions").addEventListener("click", saveQuestions);
$("theme-toggle").addEventListener("click", toggleTheme);
$("repair-type").addEventListener("change", updateRepairFields);
$("repair-run").addEventListener("click", repairEdited);
$("repair-accept").addEventListener("click", acceptRepair);
$("repair-reset").addEventListener("click", resetRepair);
$("upload-repair-file").addEventListener("change", (event) => uploadRepairImage(event.target.files?.[0]));
$("use-benchmark-repair").addEventListener("click", () => {
  uploadRepair = null;
  $("upload-repair-file").value = "";
  $("upload-state").textContent = "Using current benchmark image.";
  render();
});
$("author-prev").addEventListener("click", () => moveAuthor(-1));
$("author-next").addEventListener("click", () => moveAuthor(1));
$("author-upload-trigger").addEventListener("click", () => $("author-upload-files").click());
$("author-upload-files").addEventListener("change", (event) => uploadAuthoringImages(event.target.files));
$("author-jump").addEventListener("change", () => {
  authoring.cursor = Math.max(0, Math.min(authoring.items.length - 1, Number($("author-jump").value) - 1));
  authoring.box = null;
  authoring.candidate = null;
  authoring.draft = null;
  renderAuthoring();
});
$("author-clear-box").addEventListener("click", () => { authoring.box = null; authorBoxStyle(); });
$("author-run-edit").addEventListener("click", runAuthorEdit);
$("author-save-item").addEventListener("click", saveAuthorItem);
$("author-export").addEventListener("click", exportAuthoring);
$("author-default-questions").addEventListener("click", () => renderAuthorQuestions(authorDefaultQuestions()));
$("author-add-question").addEventListener("click", () => {
  renderAuthorQuestions([
    ...collectAuthorQuestions(),
    { probe:"custom", image_role:"base", question_type:"open_generation", question:"", answer:"", eval_type:"manual" }
  ]);
});
document.querySelectorAll(".mode-tab").forEach((button) => button.addEventListener("click", () => updateMode(button.dataset.mode)));
statusButtons.forEach((button) => button.addEventListener("click", () => {
  rejectReasonOpen = false;
  save(button.dataset.status, true);
}));
$("reject-toggle").addEventListener("click", () => {
  rejectReasonOpen = !rejectReasonOpen;
  const item = filtered[cursor];
  renderDecisionControls(item ? decisions[item.review_id]?.status : "");
});
document.querySelectorAll(".clear-box").forEach((button) => button.addEventListener("click", () => {
  const kind = button.dataset.kind;
  if (activeMode === "repair" && kind === "base") return;
  if (uploadRepair && activeMode === "repair" && kind === "edited") {
    uploadRepair.bbox = null;
    currentBoxes.edited = null;
    boxStyle(kind);
    return;
  }
  currentBoxes[kind] = null;
  boxStyle(kind);
  save(null, false);
}));
document.addEventListener("keydown", (event) => {
  if (event.target.matches("textarea,input,select")) return;
  const keys = { a:"keep", r:"needs_repair", x:"reject_edit_failed", b:"reject_base_invalid", u:"unsure" };
  if (keys[event.key.toLowerCase()]) {
    rejectReasonOpen = false;
    save(keys[event.key.toLowerCase()], true);
  }
  if (event.key === "ArrowLeft") move(-1);
  if (event.key === "ArrowRight") move(1);
});

applyTheme(currentTheme());
init().catch((error) => { $("summary").textContent = `Load failed: ${error}`; });
