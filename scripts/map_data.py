#!/usr/bin/env python

try:
    import cPickle as pickle
except ImportError:
    import pickle

import rospy
import overpy
import shapely.geometry as geometry
from shapely.prepared import prep
from shapely.ops import linemerge
import os
import utm
import numpy as np
from random import random
import time
from points_to_graph_points import points_to_graph_points, points_arr_to_point_line, get_point_line

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iter, desc=None, *args, **kwargs):
        if desc is not None:
            rospy.loginfo(desc)
        return iter

import gpxpy
import gpxpy.gpx

from graph_search_params import *
from way import Way


OBSTACLE_RADIUS = 2


class MapData:
    def __init__(self, coords, coords_type="file", current_robot_position=None, flip=False):
        self.api = overpy.Overpass(url="https://overpass.kumi.systems/api/interpreter")

        self.coords_type = coords_type
        if coords_type == "file":
            gpx_f = open(coords, 'r')
            gpx_object = gpxpy.parse(gpx_f)
            self.waypoints = np.array([[point.latitude, point.longitude] for point in gpx_object.waypoints])
            self.waypoints, self.zone_number, self.zone_letter = self.waypoints_to_utm(self.waypoints)
        elif coords_type == "array":
            self.waypoints = np.array(coords[0])
            self.zone_number = coords[1]
            self.zone_letter = coords[2]
        else:
            rospy.logerr("Unknown coords_type.")
            return

        self.flip = flip
        if self.flip:
            self.waypoints = np.flip(self.waypoints, 0)

        if current_robot_position is not None:
            self.robot_position_first_point = True
            self.waypoints = np.concatenate([current_robot_position, self.waypoints])
        else:
            self.robot_position_first_point = False
        
        self.max_x = np.max(self.waypoints[:,0]) + RESERVE
        self.min_x = np.min(self.waypoints[:,0]) - RESERVE
        self.max_y = np.max(self.waypoints[:,1]) + RESERVE
        self.min_y = np.min(self.waypoints[:,1]) - RESERVE

        self.max_lat = utm.to_latlon(self.max_x + OSM_RECTANGLE_MARGIN, self.max_y + OSM_RECTANGLE_MARGIN, self.zone_number, self.zone_letter)[0]
        self.max_long = utm.to_latlon(self.max_x + OSM_RECTANGLE_MARGIN, self.max_y + OSM_RECTANGLE_MARGIN, self.zone_number, self.zone_letter)[1]
        self.min_lat = utm.to_latlon(self.min_x - OSM_RECTANGLE_MARGIN, self.min_y - OSM_RECTANGLE_MARGIN, self.zone_number, self.zone_letter)[0]
        self.min_long = utm.to_latlon(self.min_x - OSM_RECTANGLE_MARGIN, self.min_y - OSM_RECTANGLE_MARGIN, self.zone_number, self.zone_letter)[1]

        self.points = list(map(geometry.Point, zip(self.waypoints[:,0], self.waypoints[:,1])))

        self.points_information = []
        self.way_node_ids = set() 
        
        self.roads = set()
        self.footways = set()
        self.barriers = set()

        self.roads_list = []
        self.footways_list = []
        self.barriers_list = []

        self.ways = dict()

        self.BARRIER_TAGS = self.csv_to_dict(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'parameters/barrier_tags.csv'))
        self.NOT_BARRIER_TAGS = self.csv_to_dict(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'parameters/not_barrier_tags.csv'))
        self.ANTI_BARRIER_TAGS = self.csv_to_dict(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'parameters/anti_barrier_tags.csv'))

        self.OBSTACLE_TAGS = self.csv_to_dict(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'parameters/obstacle_tags.csv'))
        self.NOT_OBSTACLE_TAGS = self.csv_to_dict(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'parameters/not_obstacle_tags.csv'))

    def csv_to_dict(self, f):
        arr = np.genfromtxt(f, dtype=str, delimiter=',')
        dic = dict()
        for row in arr:
            if row[0] in dic:
                dic[row[0]].append(row[1])
            else:
                dic[row[0]] = [row[1]]
        return dic

    def waypoints_to_utm(self, waypoints):
        '''
        Convert waypoints obtained from .gpx file from lat/lon (WGS84) to UTM.

        Parameters:
        -----------
        waypoints : numpy.ndarray
            Array of waypoints in lat/lon format.

        Returns:
        --------
        utm_coords : numpy.ndarray
            Array of waypoints in UTM format.
        zone_number : int
            UTM zone number.
        zone_letter : str
            UTM zone letter.
        '''
        utm_arr = utm.from_latlon(waypoints[:,0], waypoints[:,1])
        utm_coords = np.concatenate((utm_arr[0].reshape(-1,1), utm_arr[1].reshape(-1,1)),axis=1)
        zone_number = utm_arr[2]
        zone_letter = utm_arr[3]
        return utm_coords, zone_number, zone_letter

    def get_way_query(self):
        query = "(way({}, {}, {}, {}); >; ); out;".format(self.min_lat, self.min_long, self.max_lat, self.max_long)
        return query
    
    def get_rel_query(self):
        query = "(way({}, {}, {}, {}); <; ); out;".format(self.min_lat, self.min_long, self.max_lat, self.max_long)
        return query
    
    def get_node_query(self):
        query = "(node({}, {}, {}, {}); ); out;".format(self.min_lat, self.min_long, self.max_lat, self.max_long)
        return query

    def run_queries(self):
        '''
        Obtain data from OSM through their API.
        '''
        def make_overpy_result_picklable(data):
            def make_overpy_picklable(data):
                if hasattr(data, '_attribute_modifiers'):
                    data._attribute_modifiers = None
                return data
            data = make_overpy_picklable(data)
            for i in range(len(data.nodes)):
                data.nodes[i] = make_overpy_picklable(data.nodes[i])
            for i in range(len(data.ways)):
                data.ways[i] = make_overpy_picklable(data.ways[i])
            for i in range(len(data.areas)):
                data.areas[i] = make_overpy_picklable(data.areas[i])
            for i in range(len(data.relations)):
                data.relations[i] = make_overpy_picklable(data.relations[i])
            return data

        break_time = 5
        tries = 1
        while tries < 4 and not rospy.is_shutdown():
            rospy.loginfo("Running 1/3 OSM query.")
            try:
                way_query = self.get_way_query()
                osm_ways_data = self.api.query(way_query)
                self.way_query = way_query
                self.osm_ways_data = make_overpy_result_picklable(osm_ways_data)
                break
            except Exception as e:
                rospy.loginfo(e)
                rospy.loginfo("--------------\nQuery failed.\nRerunning the query after {} s.".format(break_time))
                time.sleep(break_time)
                tries += 1

        tries = 1
        while tries < 4 and not rospy.is_shutdown():
            rospy.loginfo("Running 2/3 OSM query.")
            try:
                rel_query = self.get_rel_query()
                osm_rels_data = self.api.query(rel_query)
                self.rel_query = rel_query
                self.osm_rels_data = make_overpy_result_picklable(osm_rels_data)
                break
            except Exception as e:
                rospy.loginfo(e)
                rospy.loginfo("--------------\nQuery failed.\nRerunning the query after {} s.".format(break_time))
                time.sleep(break_time)
                tries += 1

        tries = 1
        while tries < 4 and not rospy.is_shutdown():
            rospy.loginfo("Running 3/3 OSM query.")
            try:  
                node_query = self.get_node_query()
                osm_nodes_data = self.api.query(node_query)
                self.node_query = node_query
                self.osm_nodes_data = make_overpy_result_picklable(osm_nodes_data)
                break
            except Exception as e:
                rospy.loginfo(e)
                rospy.loginfo("--------------\nQuery failed.\nRerunning the query after {} s.".format(break_time))
                time.sleep(break_time)
                tries += 1

        rospy.loginfo("Queries finished.")

    def parse_ways(self):
        '''
        Fill self.ways, a dictionary of id:way pairs, from all the ways from the query.
        '''
        for way in tqdm(self.osm_ways_data.ways, desc="Parse ways"):
            way_to_store = Way()
            coords = []
            is_area = False

            # Convert WGS -> UTM.
            lats = np.array([float(node.lat) for node in way.nodes])
            lons = np.array([float(node.lon) for node in way.nodes])
            utm_coords = utm.from_latlon(lats,lons)
            coords = list(zip(utm_coords[0], utm_coords[1]))

            # Keep track of IDs of each node, so that in parse_nodes we can distinguish them from solitary nodes.
            ids = [node.id for node in way.nodes]
            if self.way_node_ids is None:
                self.way_node_ids = dict()
            self.way_node_ids.update(ids)              
            
            # Distinguish areas and non-areas (we use a single class for both cases).
            if coords[0] == coords[-1]:
                is_area = True
            
            way_to_store.id = way.id
            way_to_store.is_area = is_area
            way_to_store.nodes = way.nodes
            way_to_store.tags = way.tags

            if way_to_store.tags is None:
                way_to_store.tags = dict()

            if is_area:
                way_to_store.line = geometry.Polygon(coords)
            else:
                way_to_store.line = geometry.LineString(coords)
            
            self.ways[way.id] = way_to_store

    def combine_ways(self,ids):
        ways = []
        for id in ids:
            ways.append(self.ways[id])
        i = 0
        while i < len(ways):
            j = 0
            while j < len(ways):
                if i != j:
                    if (ways[i].nodes[0].id == ways[j].nodes[0].id) and (not ways[i].is_area and not ways[j].is_area):
                        ways[i].nodes.reverse()
                    elif (ways[i].nodes[-1].id == ways[j].nodes[-1].id) and (not ways[i].is_area and not ways[j].is_area):
                        ways[j].nodes.reverse()

                    if ways[i].nodes[-1].id == ways[j].nodes[0].id and (not ways[i].is_area and not ways[j].is_area):
                        
                        combined_line = linemerge([ways[i].line, ways[j].line])

                        new_way = Way()
                        new_way.id = int(-10**15*random())
                        while new_way.id in self.ways.keys():
                            new_way.id = int(-10**15*random())
                        # tady zlobi ten update
                        new_way.nodes = ways[i].nodes + ways[j].nodes[1:] 
                        
                        if ways[i].tags is None:
                            ways[i].tags = dict()
                        if ways[j].tags is None:
                            ways[j].tags = dict()
                        new_way.tags = ways[i].tags.update(ways[j].tags)
                        new_way.line = combined_line
  
                        if new_way.nodes[0].id == new_way.nodes[-1].id:
                            new_way.is_area = True
                            new_way.line = geometry.Polygon(new_way.line.coords)
                        self.ways[new_way.id] = new_way
                        ways[j] = new_way
                        ids[j] = new_way.id
                        ids.pop(i)
                        ways.pop(i)
                        i -= 1
                        j -= 1
                        break
                j += 1
            i += 1
        
        return ids

    def parse_rels(self):
        """ 2. Needs self.ways DICTIONARY (key is id) with a self.is_area parameter, which is obtained from parse_ways.
            Use relations to alter ways - combine neighbor ways, add tags...
        """
        for rel in tqdm(self.osm_rels_data.relations, desc="Parse rels"):
            inner_ids = []
            outer_ids = []
            keys = self.ways.keys()

            # Separate inner and outer ways of relation.
            for member in rel.members:
                if member._type_value == "way":
                    if int(member.ref) in keys:
                        if member.role == "outer":
                            outer_ids.append(int(member.ref))
                        else:
                            inner_ids.append(int(member.ref))

            # If two ways are "connected" (they share a node), combine them into one.
            outer_ids = self.combine_ways(outer_ids)

            for id in outer_ids:
                way = self.ways[id]

                way.in_out = "outer"
                
                if way.tags is None:
                    way.tags = dict()
                if rel.tags is None:
                    rel.tags = dict()
                way.tags.update(rel.tags)
                self.ways[id] = way

            for id in inner_ids:
                way = self.ways[id]
                way.in_out = "inner"
                self.ways[id] = way

    def parse_nodes(self):
        """ Convert solitary nodes (not part of a way) to barrier areas. """

        for node in tqdm(self.osm_nodes_data.nodes, desc="Parse nodes"):
            if not node.id in self.way_node_ids:
                # Check if node is a obstacle.
                if any(key in self.OBSTACLE_TAGS and (node.tags[key] in self.OBSTACLE_TAGS[key] or ('*' in self.OBSTACLE_TAGS[key] and not node.tags[key]  in self.NOT_OBSTACLE_TAGS.get(key,[]))) for key in node.tags):
                    obstacle = Way()
                    obstacle.id = node.id
                    obstacle.is_area = True
                    obstacle.tags = node.tags

                    coords = utm.from_latlon(float(node.lat),float(node.lon))
                    point = geometry.Point([coords[0], coords[1]])
                    polygon = self.point_to_polygon(point, OBSTACLE_RADIUS)
                    obstacle.line = polygon

                    self.barriers.add(obstacle)

    def point_to_polygon(self, point, r):
        """ Convert a node (= a point) to a circle area, with a given radius."""

        polygon = point.buffer(r)
        return polygon

    def run_parse(self):
        '''
        Parse OSM data into their respective categories.
        '''
        rospy.loginfo("Running analysis.")
        self.parse_ways()
        self.parse_rels()
        self.parse_nodes()

        self.separate_ways()
        self.sets_to_lists()
        rospy.loginfo("Analysis finished.")
