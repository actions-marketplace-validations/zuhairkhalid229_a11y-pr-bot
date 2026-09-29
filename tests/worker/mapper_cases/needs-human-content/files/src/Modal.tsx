import { XIcon } from './icons'

export function Modal({ children, onDismiss }: Props) {
  return (
    <div role="dialog" className="modal">
      <button className="modal-close" onClick={onDismiss}>
        <XIcon />
      </button>
      {children}
    </div>
  )
}
