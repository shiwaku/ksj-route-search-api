-- KSJ 比較用の最小プロファイル: 速度は maxspeed タグそのまま・無向・ターンペナルティなし（scipy と同じモデル）
api_version = 4

function setup()
  return {
    properties = {
      max_speed_for_map_matching = 80/3.6,
      weight_name = 'duration',
      use_turn_restrictions = false,
      continue_straight_at_waypoint = false,
    },
    default_mode = mode.driving,
    default_speed = 20,
  }
end

function process_node(profile, node, result) end

function process_way(profile, way, result)
  local hw = way:get_value_by_key('highway')
  if not hw or hw == '' then return end
  local sp = tonumber(way:get_value_by_key('maxspeed')) or profile.default_speed
  result.forward_mode = mode.driving
  result.backward_mode = mode.driving
  result.forward_speed = sp
  result.backward_speed = sp
  result.name = way:get_value_by_key('ksj:link_id') or ''
end

function process_turn(profile, turn)
  turn.duration = 0
  turn.weight = 0
end

return {
  setup = setup,
  process_way = process_way,
  process_node = process_node,
  process_turn = process_turn,
}
