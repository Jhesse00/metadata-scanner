(function () {
    const MIN_SINGLE_SCAN_DURATION = 2800;
    const MIN_BATCH_SCAN_DURATION = 4600;
    const ERROR_DISPLAY_DELAY = 700;
    const STEP_INTERVAL = 650;
    const SCAN_STEPS = [
        "Preparing file",
        "Reading file details",
        "Extracting metadata",
        "Checking privacy risks",
        "Creating risk score",
        "Preparing reports",
        "Sanitizing file if supported",
    ];
    const STEP_MESSAGES = [
        "Preparing your file for scanning...",
        "Reading file metadata...",
        "Extracting metadata from supported fields...",
        "Checking for sensitive fields...",
        "Creating the privacy risk score...",
        "Preparing your report...",
        "Sanitizing supported files before results load...",
    ];

    const form = document.querySelector("[data-scan-form]");
    if (!form) {
        return;
    }

    const fileInput = form.querySelector("#files");
    const submitButton = form.querySelector("[data-scan-button]");
    const progressCard = document.querySelector("[data-scan-progress]");
    const progressTitle = document.querySelector("[data-progress-title]");
    const progressMessage = document.querySelector("[data-progress-message]");
    const progressCount = document.querySelector("[data-progress-count]");
    const fileSummary = document.querySelector("[data-file-summary]");
    const steps = Array.from(document.querySelectorAll("[data-step]"));

    let hasSubmitted = false;
    let stepTimer = null;
    let activeStep = 0;
    let activeFile = 1;

    const selectedCount = () => (fileInput && fileInput.files ? fileInput.files.length : 0);
    const wait = (duration) => new Promise((resolve) => window.setTimeout(resolve, duration));

    const remainingDelay = (startedAt, minimumDuration) => {
        const elapsed = performance.now() - startedAt;
        return Math.max(0, minimumDuration - elapsed);
    };

    const updateFileSummary = () => {
        const count = selectedCount();
        if (!fileSummary) {
            return;
        }
        if (count === 0) {
            fileSummary.textContent = "Maximum upload size is 10MB per request.";
        } else if (count === 1) {
            fileSummary.textContent = `Selected: ${fileInput.files[0].name}`;
        } else {
            fileSummary.textContent = `${count} files selected for batch scanning.`;
        }
    };

    const setStep = (stepIndex) => {
        steps.forEach((step, index) => {
            step.classList.toggle("is-complete", index < stepIndex);
            step.classList.toggle("is-active", index === stepIndex);
        });
        if (progressMessage) {
            progressMessage.textContent = STEP_MESSAGES[stepIndex] || STEP_MESSAGES[STEP_MESSAGES.length - 1];
        }
    };

    const setProgressCount = (fileCount) => {
        if (!progressCount) {
            return;
        }
        progressCount.textContent = fileCount > 1 ? `Scanning file ${activeFile} of ${fileCount}` : "Working";
    };

    const advanceStep = (fileCount) => {
        activeStep += 1;
        if (activeStep >= SCAN_STEPS.length) {
            activeStep = 0;
            activeFile = Math.min(fileCount, activeFile + 1);
        }
        setStep(activeStep);
        setProgressCount(fileCount);
    };

    const showProgress = (fileCount) => {
        progressCard.hidden = false;
        progressCard.classList.remove("scan-progress-error");
        progressCard.scrollIntoView({ behavior: "smooth", block: "center" });

        activeStep = 0;
        activeFile = 1;
        if (progressTitle) {
            progressTitle.textContent = fileCount > 1 ? "Scanning selected files" : "Scanning selected file";
        }
        setStep(activeStep);
        setProgressCount(fileCount);

        stepTimer = window.setInterval(() => advanceStep(fileCount), STEP_INTERVAL);
    };

    const stopProgressTimer = () => {
        if (stepTimer) {
            window.clearInterval(stepTimer);
            stepTimer = null;
        }
    };

    const resetSubmitState = () => {
        hasSubmitted = false;
        submitButton.disabled = false;
        submitButton.textContent = "Scan Metadata";
    };

    const showError = (message) => {
        stopProgressTimer();
        resetSubmitState();
        progressCard.hidden = false;
        progressCard.classList.add("scan-progress-error");
        if (progressTitle) {
            progressTitle.textContent = "Scan failed";
        }
        if (progressMessage) {
            progressMessage.textContent = message || "Scan failed. Please try again or use a supported file type.";
        }
        if (progressCount) {
            progressCount.textContent = "Error";
        }
    };

    const renderHtmlResponse = (html, url) => {
        if (url) {
            window.history.replaceState({}, "", url);
        }
        document.open();
        document.write(html);
        document.close();
    };

    const submitWithProgress = async (startedAt, fileCount) => {
        const minimumDuration = fileCount > 1 ? MIN_BATCH_SCAN_DURATION : MIN_SINGLE_SCAN_DURATION;
        const formData = new FormData(form);
        const response = await window.fetch(form.action || window.location.href, {
            body: formData,
            method: form.method || "POST",
        });
        const html = await response.text();
        const isErrorPage = html.includes("flash-danger") || html.includes("flash-warning");
        const displayDelay = isErrorPage ? ERROR_DISPLAY_DELAY : minimumDuration;

        await wait(remainingDelay(startedAt, displayDelay));
        stopProgressTimer();
        renderHtmlResponse(html, response.url);
    };

    if (fileInput) {
        fileInput.addEventListener("change", updateFileSummary);
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (hasSubmitted) {
            return;
        }

        const fileCount = selectedCount();
        if (fileCount === 0) {
            showError("Choose at least one supported file before scanning.");
            return;
        }

        hasSubmitted = true;
        submitButton.disabled = true;
        submitButton.textContent = "Scanning...";
        const startedAt = performance.now();
        showProgress(fileCount);

        try {
            await submitWithProgress(startedAt, fileCount);
        } catch (_error) {
            await wait(ERROR_DISPLAY_DELAY);
            showError("Scan failed. Please try again or use a supported file type.");
        }
    });
})();
