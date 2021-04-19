
import itertools
import datetime
import util
import asyncio
import random
import os
import sys
import traceback
import subprocess
import time
import json
import urllib.request
import ast

# ========================================================================

def peer_to_multiaddr(peer):
  return '/ip4/{}/tcp/{}/p2p/{}'.format(
    peer['host'],
    peer['libp2p_port'],
    peer['peer_id'] )

def collect_node_status_metrics(v1, namespace, nodes_synced_near_best_tip, nodes_synced, nodes_queried, nodes_responded, nodes_errored, context_deadline_exceeded, failed_security_protocol_negotiation, connection_refused_errors, size_limit_exceeded_errors, timed_out_errors, stream_reset_errors, other_connection_errors, prover_errors):
  print('collecting node status metrics')

  pods = v1.list_namespaced_pod(namespace, watch=False)
  pod_names = [ p['metadata']['name'] for p in pods.to_dict()['items'] ]

  seeds = [ p for p in pod_names if 'seed' in p ]

  seed = random.choice(seeds)

  seed_pod = [ p for p in pods.to_dict()['items'] if p['metadata']['name'] == seed ][0]
  seed_daemon_container = [ c for c in seed_pod['spec']['containers'] if c['args'][0] == 'daemon' ][0]
  seed_vars_dict = [ v for v in seed_daemon_container['env'] ]
  seed_daemon_port = [ v['value'] for v in seed_vars_dict if v['name'] == 'DAEMON_CLIENT_PORT'][0]

  peers, response_count, errored_responses = crawl_for_peers(v1, namespace, seed, seed_daemon_port)

  err_context_deadline = 0
  err_negotiate_security_protocol = 0
  err_connection_refused = 0
  err_time_out = 0
  err_stream_reset = 0
  err_size_limit_exceeded = 0
  err_others = 0

  for p in errored_responses:
    try:
      error_str = p['error']['string']
      if 'context deadline exceeded' in error_str:
        #{'error': {'commit_id': 'baffb589965aa0a8552dca15e209d2a011af3d21', 'string': 'RPC #369385 failed: "context deadline exceeded"'}}
        err_context_deadline += 1
      elif 'failed to negotiate security protocol' in error_str:
        #{'error': {'commit_id': 'baffb589965aa0a8552dca15e209d2a011af3d21', 'string': 'RPC #369384 failed: "failed to dial 12D3KooWEsc3KyWrxmDt8J8cBXBwztRrLcYrPKdJXWU4YLdC8z5z: all dials failed\\n  * [/ip4/185.25.49.250/tcp/8302] failed to negotiate security protocol: peer id mismatch: expected 12D3KooWEsc3KyWrxmDt8J8cBXBwztRrLcYrPKdJXWU4YLdC8z5z, but remote key matches 12D3KooWBLcxkHd3KQGeLiNgwVQ8ViEb5EYg3cmSjQs5tDDXQfQb"'}}
        err_negotiate_security_protocol += 1
      elif 'connection refused' in error_str:
        #{'error': {'commit_id': 'baffb589965aa0a8552dca15e209d2a011af3d21', 'string': 'RPC #369418 failed: "failed to dial 12D3KooWKWzRb7BN7J3zXF6PkRn3sJMRBxvq58ujoTHSUHcNmWdc: all dials failed\\n  * [/ip4/178.170.47.23/tcp/35592] dial tcp4 178.170.47.23:35592: connect: connection refused"'}}
        err_connection_refused +=1
      elif 'timed out requesting node status data from peer' in error_str:
        err_time_out += 1
      elif 'node status data was greater than' in error_str:
        print("Errored response: {}".format(error_str))
        err_size_limit_exceeded +=1
      elif 'stream reset' in error_str:
        err_stream_reset += 1
      else:
        print("Errored response: {}".format(error_str))
        err_others += 1
    except _:
      print("Errored response: {}".format(error_str))
      err_others += 1

  num_peers = len(peers.values())

  synced_fraction = sum([ p['sync_status'] == 'Synced' for p in peers.values() ]) / num_peers

  nodes_queried.set(response_count)
  nodes_responded.set(num_peers)
  nodes_errored.set(len(errored_responses))
  context_deadline_exceeded.set(err_context_deadline)
  failed_security_protocol_negotiation.set(err_negotiate_security_protocol)
  connection_refused_errors.set(err_connection_refused)
  stream_reset_errors.set(err_stream_reset)
  size_limit_exceeded_errors.set(err_size_limit_exceeded)
  timed_out_errors.set(err_time_out)
  other_connection_errors.set(err_others)
  nodes_synced.set(synced_fraction)

  # -------------------------------------------------

   # TODO: prover_erros

  # -------------------------------------------------

  # note: k_block_hashes_and_timestamps is most recent last
  chains = [ p['k_block_hashes_and_timestamps'] for p in peers.values() ]

  tree = {}
  parents = {}
  for c in chains:
    for (parent, child) in zip(c, c[1:]):
      parent = parent[0]
      child = child[0]
      tree.setdefault(parent, set())
      tree[parent].add(child)
      parents[child] = parent

  blocks = set(itertools.chain(tree.keys(), *tree.values()))
  roots = [ b for b in blocks if b not in parents.keys() ]

  def get_deepest_child(p):
    children_and_depths = []
    children_and_depths.append((p, 0))
    if p in tree:
      for c in tree[p]:
        child, depth = get_deepest_child(c)
        children_and_depths.append((child, depth + 1))
    return max(children_and_depths, key=lambda x: x[1])

  #get the latest protocol states of each node and the length of the chain (to eliminate nodes that are newly joining or restarting without persisted frontier)
  latest_protocol_states = [c[len(c)-1][0] for c in chains if len(c) >= 290 and len(c) > 0]
  common_states = {}
  for state_hash in latest_protocol_states:
    if state_hash in common_states:
      common_states[state_hash] = common_states[state_hash] + 1
    else:
      common_states[state_hash] = 1

  print("Best protocol states and the number of nodes synced to it:{}".format(common_states))

  most_common_best_protocol_state,_ = max(common_states.items(), key=lambda x: x[1])

  n = 3
  last_n_protocol_states = [ most_common_best_protocol_state ]
  for i in range(n):
    parent = last_n_protocol_states[-1]
    if parent in parents:
      last_n_protocol_states.append(parents[parent])

  print("Latest {} protocol states:{}".format(n+1, last_n_protocol_states))

  any_hash_in_last_n = lambda peer: any([ p_hash in last_n_protocol_states for p_hash, _ in peer['k_block_hashes_and_timestamps'][-n:] ])

  synced_near_best_tip_num = [ any_hash_in_last_n(p) for p in peers.values() if p['sync_status'] == 'Synced' ]

  #don't include nodes that are in catchup or bootstrap state
  all_synced_peers = [ p['sync_status'] == 'Synced' for p in peers.values() ]

  synced_near_best_tip_fraction = sum(synced_near_best_tip_num) / sum(all_synced_peers)

  peers_out_of_sync=[("peer-id:"+p['node_peer_id'], "state-hash:"+p['protocol_state_hash'], "status:"+p['sync_status']) for p in peers.values() if not any_hash_in_last_n(p) and p['sync_status'] == 'Synced']

  print("Number of  peers with 'Synced' status: {}\nPeers not synced near the best tip: {}".format(sum(all_synced_peers), peers_out_of_sync))

  nodes_synced_near_best_tip.set(synced_near_best_tip_fraction)

# ========================================================================

def crawl_for_peers(v1, namespace, seed, seed_daemon_port, max_crawl_requests=10):

  peer_table = {}

  queried_peers = set()
  unqueried_peers = {}
  error_resps = []
  all_resps = []

  def contains_error(resp):
    try:
      resp['error']
      return True
    except KeyError :
      return False

  def no_error(resp):
    return (not (contains_error(resp)))

  def add_resp(resp, direct_queried_peers):
    # we use ast instead of json to handle properties with single quotes instead of double quotes (which the response seems to often contain)
    resps = [ ast.literal_eval(s) for s in resp.split('\n') if s != '' ]

    peers = list(filter(no_error,resps))
    error_resps.extend(list(filter(contains_error,resps)))
    all_resps.extend(resps)

    key_value_peers = [ ((p['node_ip_addr'], p['node_peer_id']), p) for p in peers ]

    for (k,v) in key_value_peers:
      if k not in peer_table:
        peer_table[k] = v

    queried_peers.update([ p['node_peer_id'] for p in peers ])
    queried_peers.update([ p['peer_id'] for p in direct_queried_peers ])
    for p in itertools.chain(*[ p['peers'] for p in peers ]):
      unqueried_peers[p['peer_id']] = p
    for p in queried_peers:
      if p in unqueried_peers:
        del unqueried_peers[p]

  cmd = "mina advanced node-status -daemon-port " + seed_daemon_port + " -daemon-peers" + " -show-errors"
  resp = util.exec_on_pod(v1, namespace, seed, 'coda', cmd)
  add_resp(resp, [])

  requests = 0

  while len(unqueried_peers) > 0 and requests < max_crawl_requests:
    peers_to_query = list(unqueried_peers.values())
    peers = ','.join(peer_to_multiaddr(p) for p in peers_to_query)

    print ('Queried ' + str(len(queried_peers)) + ' peers. Gathering node status on %s unqueried peers'%(str(len(unqueried_peers))))

    resp = util.exec_on_pod(v1, namespace, seed, 'coda', "mina advanced node-status -daemon-port " + seed_daemon_port + " -peers " + peers + " -show-errors")
    add_resp(resp, peers_to_query)

    requests += 1

  return (peer_table, len(all_resps), error_resps)

# ========================================================================
