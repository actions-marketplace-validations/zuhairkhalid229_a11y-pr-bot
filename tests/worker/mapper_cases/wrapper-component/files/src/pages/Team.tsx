import { Avatar } from '../components/Avatar'
import { team } from '../data/team'

export default function Team() {
  return (
    <ul className="team-grid">
      {team.map((person) => (
        <li key={person.id}>
          <Avatar src={person.photo} size={64} />
          <span>{person.name}</span>
        </li>
      ))}
    </ul>
  )
}
