// Click a message image to see it larger, in a dialog.
//
// One <dialog>, created on first use; nothing is added per image, and the
// handlers are delegated from `document`, so images a live update brings
// in work like the rest. `showModal()` gives the top layer, ESC and focus
// containment; the script adds the backdrop click and the × button, and
// puts focus back on the image when the dialog closes.
//
// Zoom is bounded on both sides: never below fit-to-frame, never past the
// image's natural size — a zoom past it would only show interpolated
// pixels. So an image already shown at its natural size has nothing to
// offer and does not open. Nor does an image inside a link: a click there
// already means "follow the link", and the author chose that.
//
// The dialog is a transparent layer over the whole viewport. The frame
// in its middle is the image at fit scale, at most 90% of the viewport
// each way, so at any zoom the image covers the frame and panning is
// clamped to keep it so; the × sits in the viewport's corner, in the
// margin that leaves, wherever the zoom has moved the picture. The geometry is plain numbers: `scale` and the image's
// top-left offset (`x`, `y`) in the frame, applied as one transform.
(function () {
    'use strict';

    const MAX_FRACTION = 0.9;   // of the viewport, each way
    const WHEEL_RATE = 0.0015;  // zoom factor per wheel delta unit (exp)

    let dialog = null;
    let frame = null;
    let zoomImg = null;
    let source = null;          // the page image the dialog was opened from
    let nat = { w: 0, h: 0 };   // natural size
    let box = { w: 0, h: 0 };   // frame size
    let fit = 1;                // scale at which the image fills the frame
    let scale = 1;
    let x = 0;
    let y = 0;

    function zoomable(img) {
        if (!img.closest('#transcript .message')) return false;
        if (img.closest('a, summary, dialog') || img.classList.contains('artifact-favicon')) return false;
        if (!img.complete || !img.naturalWidth || !img.naturalHeight) return false;
        const shown = img.getBoundingClientRect();
        return img.naturalWidth > shown.width + 1 || img.naturalHeight > shown.height + 1;
    }

    function viewport() {
        return {
            w: document.documentElement.clientWidth || window.innerWidth,
            h: window.innerHeight,
        };
    }

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    // Keep the image covering the frame: its offset can only range over
    // the part of it that overhangs.
    function clampPan() {
        x = clamp(x, box.w - nat.w * scale, 0);
        y = clamp(y, box.h - nat.h * scale, 0);
    }

    function apply() {
        zoomImg.style.transform = 'translate(' + x + 'px, ' + y + 'px) scale(' + scale + ')';
        frame.classList.toggle('cc-zoom-pannable', scale > fit + 1e-6);
    }

    // Size the frame to the image at fit scale within 90% of the viewport.
    // On a resize the zoom keeps its ratio to fit and the point at the
    // frame's centre stays there, then both are clamped again.
    function layout(keepView) {
        const vp = viewport();
        const maxW = Math.floor(vp.w * MAX_FRACTION);
        const maxH = Math.floor(vp.h * MAX_FRACTION);
        const oldFit = fit;
        const centre = keepView
            ? { u: (box.w / 2 - x) / scale, v: (box.h / 2 - y) / scale }
            : null;
        fit = Math.min(maxW / nat.w, maxH / nat.h, 1);
        box = { w: Math.round(nat.w * fit), h: Math.round(nat.h * fit) };
        frame.style.width = box.w + 'px';
        frame.style.height = box.h + 'px';
        if (centre) {
            scale = clamp(scale * fit / oldFit, fit, 1);
            x = box.w / 2 - centre.u * scale;
            y = box.h / 2 - centre.v * scale;
        } else {
            scale = fit;
            x = 0;
            y = 0;
        }
        clampPan();
        apply();
    }

    function zoomAt(px, py, next) {
        next = clamp(next, fit, 1);
        x = px - (px - x) * next / scale;
        y = py - (py - y) * next / scale;
        scale = next;
        clampPan();
        apply();
    }

    function onResize() {
        if (dialog && dialog.open) layout(true);
    }

    function build() {
        dialog = document.createElement('dialog');
        dialog.className = 'cc-zoom';
        dialog.setAttribute('aria-label', 'Zoomed image');
        // Focusable, and first in the dialog, so `showModal` focuses the
        // frame rather than ringing the × on every mouse open; Tab still
        // reaches the ×.
        frame = document.createElement('div');
        frame.className = 'cc-zoom-frame';
        frame.tabIndex = -1;
        zoomImg = document.createElement('img');
        zoomImg.draggable = false;
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'cc-zoom-close';
        close.setAttribute('aria-label', 'Close');
        close.textContent = '×';
        frame.appendChild(zoomImg);
        dialog.appendChild(frame);
        dialog.appendChild(close);
        document.body.appendChild(dialog);

        close.addEventListener('click', function () { dialog.close(); });

        // The layer around the frame is the dialog element itself, so a
        // click whose target is the dialog was outside the frame. A drag
        // that ends outside the frame is not one: the frame holds the
        // pointer capture.
        dialog.addEventListener('click', function (event) {
            if (event.target === dialog) dialog.close();
        });

        dialog.addEventListener('close', function () {
            window.removeEventListener('resize', onResize);
            zoomImg.removeAttribute('src');
            if (source && source.isConnected) {
                if (!source.hasAttribute('tabindex')) source.setAttribute('tabindex', '-1');
                source.focus({ preventScroll: true });
            }
            source = null;
        });

        // Over the frame the wheel zooms about the cursor; anywhere in the
        // dialog (the backdrop included) it never scrolls the page behind.
        dialog.addEventListener('wheel', function (event) {
            event.preventDefault();
            const r = frame.getBoundingClientRect();
            const px = event.clientX - r.left;
            const py = event.clientY - r.top;
            if (px < 0 || py < 0 || px > r.width || py > r.height) return;
            zoomAt(px, py, scale * Math.exp(-event.deltaY * WHEEL_RATE));
        }, { passive: false });

        let drag = null;
        frame.addEventListener('pointerdown', function (event) {
            if (event.button !== 0) return;
            if (scale <= fit + 1e-6) return;
            event.preventDefault();
            frame.setPointerCapture(event.pointerId);
            drag = { id: event.pointerId, sx: event.clientX, sy: event.clientY, x: x, y: y };
            frame.classList.add('cc-zoom-panning');
        });
        frame.addEventListener('pointermove', function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            x = drag.x + event.clientX - drag.sx;
            y = drag.y + event.clientY - drag.sy;
            clampPan();
            apply();
        });
        function endDrag(event) {
            if (!drag || event.pointerId !== drag.id) return;
            drag = null;
            frame.classList.remove('cc-zoom-panning');
        }
        frame.addEventListener('pointerup', endDrag);
        frame.addEventListener('pointercancel', endDrag);
    }

    function open(img) {
        if (!dialog) build();
        source = img;
        nat = { w: img.naturalWidth, h: img.naturalHeight };
        zoomImg.alt = img.alt || '';
        zoomImg.src = img.currentSrc || img.src;
        zoomImg.style.width = nat.w + 'px';
        zoomImg.style.height = nat.h + 'px';
        dialog.showModal();
        layout(false);
        window.addEventListener('resize', onResize);
    }

    // The cursor says whether a click will open: only the rendered size
    // tells, and it changes with the window, so it is decided on hover.
    document.addEventListener('mouseover', function (event) {
        const img = event.target;
        if (img.tagName !== 'IMG' || !img.closest('#transcript')) return;
        img.classList.toggle('cc-zoomable', zoomable(img));
    });

    document.addEventListener('click', function (event) {
        const img = event.target;
        if (img.tagName !== 'IMG' || event.button !== 0 || !zoomable(img)) return;
        event.preventDefault();
        open(img);
    });
})();
