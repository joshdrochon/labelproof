/**
 * Picking the label pictures.
 *
 * Drag-and-drop is the fast path for the people who already work that way; the visible
 * **Choose files** button is the path for everyone else, and it is a real button — the
 * whole flow works with a keyboard and never depends on knowing that a dashed rectangle
 * is secretly clickable.
 *
 * The panel this lives in is a dropzone *or* an evidence viewer, never both at once. See
 * the note at the top of `EvidenceOverlay.tsx`.
 *
 * File problems are caught here and said in plain words, before anything is sent: a
 * 40MB photo should not cost the agent a round trip to be told no (UX-6, LP-076).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const MAX_FILES = 4;
const MAX_BYTES = 10 * 1024 * 1024;

const ACCEPTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.pdf'];
const ACCEPTED_PATTERN = /\.(jpe?g|png|webp|heic|heif|pdf)$/i;

function isAccepted(file: File): boolean {
  if (file.type.startsWith('image/')) return true;
  if (file.type === 'application/pdf') return true;
  // HEIC often arrives with an empty type, so fall back to the name.
  return ACCEPTED_PATTERN.test(file.name);
}

function describeSize(bytes: number): string {
  // KB below a megabyte. A 37 KB file rendered as "0.0MB" reads as a failed upload —
  // which is exactly how it looked to the first person who used this screen, on a real
  // label photograph that had uploaded perfectly.
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface DropzoneProps {
  files: File[];
  onChange: (files: File[]) => void;
  disabled?: boolean;
}

export default function Dropzone({ files, onChange, disabled = false }: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const previews = useMemo(
    () =>
      files.map((file) => ({
        name: file.name,
        size: file.size,
        isPdf: file.type === 'application/pdf' || /\.pdf$/i.test(file.name),
        url: file.type.startsWith('image/') ? URL.createObjectURL(file) : null,
      })),
    [files],
  );

  useEffect(
    () => () => {
      for (const preview of previews) if (preview.url) URL.revokeObjectURL(preview.url);
    },
    [previews],
  );

  const accept = useCallback(
    (incoming: FileList | null) => {
      if (!incoming || incoming.length === 0) return;
      const candidates = Array.from(incoming);
      const problems: string[] = [];
      const kept: File[] = [];

      for (const file of candidates) {
        if (!isAccepted(file)) {
          problems.push(`${file.name} is not a picture or a PDF, so it was left out.`);
          continue;
        }
        if (file.size > MAX_BYTES) {
          problems.push(
            `${file.name} is ${describeSize(file.size)}. The limit is 10MB — send a smaller picture of the same label.`,
          );
          continue;
        }
        kept.push(file);
      }

      const room = MAX_FILES - files.length;
      const added = kept.slice(0, Math.max(room, 0));
      if (kept.length > added.length) {
        problems.push(
          `Only ${MAX_FILES} pictures can be checked at once, so the extra ones were left out.`,
        );
      }

      setProblem(problems.length > 0 ? problems.join(' ') : null);
      if (added.length > 0) onChange([...files, ...added]);
    },
    [files, onChange],
  );

  const remove = (index: number) => {
    setProblem(null);
    onChange(files.filter((_, i) => i !== index));
  };

  return (
    <div className="dropzone-panel">
      <div
        className="dropzone"
        data-dragging={dragging ? 'true' : 'false'}
        data-disabled={disabled ? 'true' : 'false'}
        onDragOver={(event) => {
          if (disabled) return;
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          if (disabled) return;
          event.preventDefault();
          setDragging(false);
          accept(event.dataTransfer.files);
        }}
      >
        <p className="dropzone__lead">
          <span className="dropzone__step">Step 1</span>
          Pictures of the label
        </p>
        <p className="dropzone__help">
          Front and back if you have both. The government warning is usually on the back.
        </p>

        <button
          type="button"
          className="btn btn--secondary dropzone__button"
          disabled={disabled || files.length >= MAX_FILES}
          onClick={() => inputRef.current?.click()}
        >
          Choose files
        </button>

        <p className="dropzone__or">or drag them here</p>
        <p className="dropzone__limits">
          Up to {MAX_FILES} files, 10MB each. JPG, PNG, WebP, HEIC or PDF.
        </p>

        <input
          ref={inputRef}
          className="visually-hidden"
          type="file"
          multiple
          accept={ACCEPTED_EXTENSIONS.join(',')}
          disabled={disabled}
          aria-label="Choose pictures of the label"
          onChange={(event) => {
            accept(event.target.files);
            event.target.value = '';
          }}
        />
      </div>

      {problem ? (
        <p className="dropzone__problem" role="alert">
          {problem}
        </p>
      ) : null}

      {previews.length > 0 ? (
        <ul className="thumbs">
          {previews.map((preview, index) => (
            <li className="thumb" key={`${preview.name}-${index}`}>
              {preview.url ? (
                <img className="thumb__image" src={preview.url} alt="" />
              ) : (
                <span className="thumb__pdf" aria-hidden="true">
                  PDF
                </span>
              )}
              <span className="thumb__meta">
                <span className="thumb__name">{preview.name}</span>
                <span className="thumb__size">{describeSize(preview.size)}</span>
              </span>
              <button
                type="button"
                className="btn btn--quiet thumb__remove"
                onClick={() => remove(index)}
                disabled={disabled}
              >
                Remove
                <span className="visually-hidden"> {preview.name}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
