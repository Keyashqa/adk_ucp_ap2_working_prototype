import type { A2uiSurface, A2uiComponent } from '../api'

interface Props {
  surfaces: A2uiSurface[]
  onAction?: (actionName: string) => void
  disabled?: boolean
}

export default function A2uiRenderer({ surfaces, onAction, disabled }: Props) {
  const compMap = new Map<string, A2uiComponent>()
  for (const surface of surfaces) {
    if (surface.updateComponents) {
      for (const comp of surface.updateComponents.components) {
        compMap.set(comp.id, comp)
      }
    }
  }

  const render = (id: string): React.ReactNode => {
    const comp = compMap.get(id)
    if (!comp) return null

    switch (comp.component) {
      case 'Column':
        return (
          <div key={id} className="a2ui-column" data-id={id}>
            {(comp.children ?? []).map(cid => render(cid))}
          </div>
        )
      case 'Row':
        return (
          <div key={id} className="a2ui-row" data-id={id}>
            {(comp.children ?? []).map(cid => render(cid))}
          </div>
        )
      case 'Card':
        return (
          <div key={id} className="a2ui-card" data-id={id}>
            {comp.child ? render(comp.child) : null}
          </div>
        )
      case 'Text':
        return <p key={id} className="a2ui-text" data-id={id}>{comp.text ?? ''}</p>
      case 'Divider':
        return <hr key={id} className="a2ui-divider" data-id={id} />
      case 'Button': {
        const actionName = comp.action?.event?.name ?? ''
        return (
          <button
            key={id}
            className="a2ui-button"
            data-id={id}
            disabled={disabled}
            onClick={() => !disabled && onAction?.(actionName)}
          >
            {comp.child ? render(comp.child) : null}
          </button>
        )
      }
      default:
        return null
    }
  }

  return <div className="a2ui-surface">{render('root')}</div>
}
