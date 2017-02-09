# Copyright 2016 United States Government as represented by the Administrator
# of the National Aeronautics and Space Administration. All Rights Reserved.
#
# Portion of this code is Copyright Geoscience Australia, Licensed under the
# Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License
# at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# The CEOS 2 platform is licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Django specific
from celery.decorators import task
from celery.signals import worker_process_init, worker_process_shutdown
from celery import group
from celery.task.control import revoke
from .models import Query, Result, ResultType, Metadata
from data_cube_ui.models import AnimationType

import numpy as np
import xarray as xr
import collections
import gdal
import sys
import shutil
import osr
import os
import datetime
from collections import OrderedDict
from dateutil.tz import tzutc
import imageio

from utils.data_access_api import DataAccessApi
from utils.dc_utilities import get_spatial_ref, save_to_geotiff, create_cfmask_clean_mask, perform_timeseries_analysis_iterative, split_task, addition, generate_time_ranges, create_single_band_rgb
from utils.dc_water_classifier import wofs_classify
from utils.dc_tsm import tsm, mask_tsm

from data_cube_ui.utils import update_model_bounds_with_dataset, combine_metadata, cancel_task, error_with_message

# Author: AHDS
# Creation date: 2016-06-23
# Modified by:
# Last modified date:

# constants up top for easy access/modification
# hardcoded colors input path..
color_path = ['~/Datacube/data_cube_ui/utils/color_scales/ramp', '~/Datacube/data_cube_ui/utils/color_scales/au_clear_observations']

base_result_path = '/datacube/ui_results/tsm/'
base_temp_path = '/datacube/ui_results_temp/'

products = ['ls5_ledaps_', 'ls7_ledaps_', 'ls8_ledaps_']
platforms = ['LANDSAT_5', 'LANDSAT_7', 'LANDSAT_8']

#default measurements. leaves out all qa bands.
measurements = ['blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'cf_mask']

# holds the different compositing algorithms. Most/least recent, max/min ndvi, median, etc.
# all options are required. setting None to a option will have the algo/task splitting
# process disregard it.
processing_algorithms = {
    'tsm': {
        'geo_chunk_size': 0.5,
        'time_chunks': 8,
        'reverse_time': False,
        'time_slices_per_iteration': 1,
        'chunk_combination_method': addition,
        'processing_method': wofs_classify
    }
}

# Creates metadata and result objects from a query id.
# gets the query, computes metadata for the parameters and saves the model.
# uses the metadata to query the datacube for relevant data and creates the result.
# results computed in single time slices for memory efficiency, pushed into a single numpy
# array containing the total result. this is then used to create png/tifs to populate a result model.
# result model is constantly updated with progress and checked for task
# cancellation.


@task(name="perform_tsm_analysis")
def perform_tsm_analysis(query_id, user_id, single=False):

    print("Starting for query:" + query_id)
    # its fair to assume that the query_id will exist at this point, as if it wasn't it wouldn't
    # start the task.
    query = Query.objects.get(query_id=query_id, user_id=user_id)
    # if there is a matching query other than the one we're using now then do nothing.
    # the ui section has already grabbed the result from the db.
    if Result.objects.filter(query_id=query.query_id).exists():
        print("Repeat query, client will receive cached result.")
        return

    print("Got the query, creating metadata.")

    result_type = ResultType.objects.get(
        satellite_id=query.platform, result_id=query.query_type)

    # creates the empty result.
    result = query.generate_result()

    # grabs the resolution.
    if query.platform == "LANDSAT_ALL":
        product_details = dc.dc.list_products(
        )[dc.dc.list_products().name == products[0]+query.area_id]
    else:
        product_details = dc.dc.list_products(
        )[dc.dc.list_products().name == query.product]

    # wrapping this in a try/catch, as it will throw a few different errors
    # having to do with memory etc.
    try:
        # If its a combined product, get all the data dates.
        if query.platform == "LANDSAT_ALL":
            acquisitions = []
            for index in range(len(products)):
                acquisitions.extend(dc.list_acquisition_dates(platforms[index], products[index]+query.area_id, time=(query.time_start, query.time_end), longitude=(
                    query.longitude_min, query.longitude_max), latitude=(query.latitude_min, query.latitude_max)))
        else:
            acquisitions = dc.list_acquisition_dates(query.platform, query.product, time=(query.time_start, query.time_end), longitude=(
                query.longitude_min, query.longitude_max), latitude=(query.latitude_min, query.latitude_max))

        if len(acquisitions) < 1:
            error_with_message(result, "There were no acquisitions for this parameter set.", base_temp_path)
            return

        processing_options = processing_algorithms['tsm']
        #if its a single scene, load it all at once to prevent errors.
        if single:
            processing_options['time_chunks'] = None
            processing_options['time_slices_per_iteration'] = None

        lat_ranges, lon_ranges, time_ranges = split_task(resolution=product_details.resolution.values[0][1], latitude=(query.latitude_min, query.latitude_max), longitude=(
            query.longitude_min, query.longitude_max), acquisitions=acquisitions, geo_chunk_size=processing_options['geo_chunk_size'], time_chunks=processing_options['time_chunks'], reverse_time=processing_options['reverse_time'])

        result.total_scenes = len(time_ranges)

        # Iterates through the acquisition dates with the step in acquisitions_per_iteration.
        # Uses a time range computed with the index and index+acquisitions_per_iteration.
        # ensures that the start and end are both valid.
        print("Getting data and creating tsm")
        # create a temp folder that isn't on the nfs server so we can quickly
        # access/delete.
        if not os.path.exists(base_temp_path + query.query_id):
            os.mkdir(base_temp_path + query.query_id)
            os.chmod(base_temp_path + query.query_id, 0o777)

        print("Time chunks: " + str(len(time_ranges)))
        print("Geo chunks: " + str(len(lat_ranges)))
        # create a group of geographic tasks for each time slice.
        time_chunk_tasks = [group(generate_tsm_chunk.s(time_range_index, geographic_chunk_index, processing_options=processing_options, query=query, acquisition_list=time_ranges[time_range_index], lat_range=lat_ranges[
                                  geographic_chunk_index], lon_range=lon_ranges[geographic_chunk_index], measurements=measurements) for geographic_chunk_index in range(len(lat_ranges))).apply_async() for time_range_index in range(len(time_ranges))]

        dataset_out_water = None
        dataset_out_tsm = None
        acquisition_metadata = {}
        animation_tile_count = 0
        time_range_index = 0
        for geographic_group in time_chunk_tasks:
            full_dataset = None
            tiles = []
            # get the geographic chunk data and drop all None values
            while not geographic_group.ready():
                result.refresh_from_db()
                if result.status == "CANCEL":
                    #revoke all tasks. Running tasks will continue to execute.
                    for task_group in time_chunk_tasks:
                        for child in task_group.children:
                            child.revoke()
                    cancel_task(query, result, base_temp_path)
                    return
            group_data = [data for data in geographic_group.get()
                          if data is not None]
            result.scenes_processed += 1
            result.save()
            print("Got results for a time slice, computing intermediate product..")

            acquisition_metadata = combine_metadata(acquisition_metadata, [tile[2] for tile in group_data])
            dataset_water = xr.concat(reversed([xr.open_dataset(tile[0]) for tile in group_data]), dim='latitude').load()
            dataset_tsm = xr.concat(reversed([xr.open_dataset(tile[1]) for tile in group_data]), dim='latitude').load()

            # combine all the intermediate products for the animation creation.
            if query.animated_product != "None":
                print("Num of slices in this chunk: " +
                    str(len(time_ranges[time_range_index])))
                for timeslice in range(len(time_ranges[time_range_index])):
                  result.refresh_from_db()
                  if result.status == "CANCEL":
                      #revoke all tasks. Running tasks will continue to execute.
                      for task_group in time_chunk_tasks:
                          for child in task_group.children:
                              child.revoke()
                      cancel_task(query, result, base_temp_path)
                      return
                  nc_paths = [base_temp_path + query.query_id + '/' + \
                      str(time_range_index) + '/' + \
                      str(geoslice) + str(timeslice) + ".nc" for geoslice in range(len(lat_ranges))]

                  animated_data = xr.concat(reversed([xr.open_dataset(nc_path) for nc_path in nc_paths]), dim='latitude').load()
                  #combine the timeslice vals with the intermediate for the true value @ that timeslice
                  if time_range_index > 0 and query.animated_product != "scene":
                      animated_data = processing_options['chunk_combination_method'](animated_data, dataset_out_tsm)

                  # save to .nc for later tiff + png conversion after masking with final wofs
                  animated_data.to_netcdf(base_temp_path + query.query_id +
                              '/' + str(animation_tile_count) + ".nc")
                  # remove all the intermediates for this timeslice
                  for path in nc_paths:
                      os.remove(path)
                  animated_data = None
                  animation_tile_count += 1

            #shutil.rmtree(base_temp_path + query.query_id +
            #          '/' + str(time_range_index))

            #add this intermediate product to the total.
            dataset_out_water = processing_options[
                'chunk_combination_method'](dataset_water, dataset_out_water)
            dataset_out_tsm = processing_options[
                'chunk_combination_method'](dataset_tsm, dataset_out_tsm)
            time_range_index += 1

        latitude = dataset_out_water.latitude
        longitude = dataset_out_water.longitude

        #filter for wofs>0.8
        dataset_out = mask_tsm(dataset_out_tsm.drop('total_data'), dataset_out_water)

        geotransform = [dataset_out.longitude.values[0], product_details.resolution.values[0][1],
                        0.0, dataset_out.latitude.values[0], 0.0, product_details.resolution.values[0][0]]
        crs = str("EPSG:4326")

        # populate metadata values.
        dates = list(acquisition_metadata.keys())
        dates.sort()

        meta = query.generate_metadata(
            scene_count=len(dates), pixel_count=len(latitude)*len(longitude))

        for date in reversed(dates):
            meta.acquisition_list += date.strftime("%m/%d/%Y") + ","
            meta.clean_pixels_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels']) + ","
            meta.clean_pixel_percentages_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels'] * 100 / meta.pixel_count) + ","
        meta.save()

        file_path = base_result_path + query_id
        netcdf_path = file_path + '.nc'
        tif_path = file_path + '.tif'
        result_paths = [file_path + '_average_tsm.png', file_path + '_clear_observation.png', file_path + '_animation.gif']
        result_filled_paths = [file_path + '_filled_average_tsm.png', file_path + '_filled_clear_observation.png']

        print("Creating query results.")
        if query.animated_product != "None":
            # create images for the animation here. This is done after the main process as we need the wofs result to mask
            # the data.
            # dataset_out_tsm.variable.values[dataset_out_water.normalized_data.values < 0.8] = 0
            for index in range(len(acquisitions)):
                 nc_path = base_temp_path + query.query_id + \
                             '/' + str(index) + ".nc"
                 geotiff_path = base_temp_path + query.query_id + '/' + \
                     str(index) + '.tif'
                 png_path = base_temp_path + query.query_id + \
                     '/' + str(index) + '.png'
                 animated_data = xr.open_dataset(nc_path).load()
                 if query.animated_product != "scene":
                     animated_data = mask_tsm(animated_data.drop('total_data'), dataset_out_water)
                 else:
                     animated_data.tsm.values[dataset_out_water.normalized_data.values < 0.8] = 0
                 # get metadata needed for tif creation.
                 geotransform = [dataset_out.longitude.values[0], product_details.resolution.values[0][1],
                                 0.0, dataset_out.latitude.values[0], 0.0, product_details.resolution.values[0][0]]
                 crs = str("EPSG:4326")

                 save_to_geotiff(geotiff_path, gdal.GDT_Float64, animated_data, geotransform, crs,
                                 x_pixels=animated_data.dims['longitude'], y_pixels=animated_data.dims['latitude'], band_order=["normalized_data"] if query.animated_product != "scene" else None)
                 animated_data = None

                 animated_product = AnimationType.objects.get(
                     type_id=query.animated_product)

                 create_single_band_rgb(band=animated_product.band_number, tif_path=geotiff_path, color_scale=color_path[int(animated_product.band_number) - 1], output_path=png_path, fill=result_type.fill)

            with imageio.get_writer(file_path + '_animation.gif', mode='I', duration=1.0) as writer:
                for index in range(len(acquisitions)):
                    image = imageio.imread(
                        base_temp_path + query.query_id + '/' + str(index) + '.png')
                    writer.append_data(image)
            result.animation_path = result_paths[2]

        # get rid of all intermediate products since there are a lot.
        shutil.rmtree(base_temp_path + query.query_id)
        save_to_geotiff(tif_path, gdal.GDT_Float64, dataset_out, geotransform, get_spatial_ref(crs),
                        x_pixels=dataset_out.dims['longitude'], y_pixels=dataset_out.dims['latitude'], band_order=['normalized_data', 'total_clean'])
        dataset_out.to_netcdf(netcdf_path)

        # we've got the tif, now do the png set..
        # uses gdal dem with custom color maps..
        for index in range(len(color_path)):
            create_single_band_rgb(band=(index+1), tif_path=tif_path, color_scale=color_path[index], output_path=result_paths[index], fill=result_type.fill)

        # update the results and finish up.
        update_model_bounds_with_dataset([result, meta, query], dataset_out)
        result.data_path = tif_path
        result.data_netcdf_path = netcdf_path
        result.result_path = result_paths[0]
        result.clear_observations_path = result_paths[1]
        result.status = "OK"
        result.total_scenes = len(acquisitions)
        result.save()
        print("Finished processing results")
        # all data has been processed, create results and finish up.
        query.complete = True
        query.query_end = datetime.datetime.now()
        query.save()

    except:
        error_with_message(
            result, "There was an exception when handling this query.", base_temp_path)
        raise
    # end error wrapping.

    return


@task(name="generate_tsm_chunk")
def generate_tsm_chunk(time_num, chunk_num, processing_options=None, query=None, acquisition_list=None, lat_range=None, lon_range=None, measurements=None):
    """
    responsible for generating a piece of a water_detection product. This grabs the x/y area specified in the lat/lon ranges, gets all data
    from acquisition_list, which is a list of acquisition dates, and creates the custom mosaic using the function named in processing_options.
    saves the result to disk using time/chunk num, and returns the path and the acquisition date keyed metadata.
    """

    #if the path has been removed, the task is cancelled and this is only running due to the prefetch.
    if not os.path.exists(base_temp_path + query.query_id):
        return None

    time_index = 0
    wofs_data = None
    tsm_data = None
    water_analysis = None
    tsm_analysis = None
    acquisition_metadata = {}
    print("Starting chunk: " + str(time_num) + " " + str(chunk_num))

    #dc.load doesn't support generators so do it this way.
    time_ranges = list(generate_time_ranges(acquisition_list, processing_options['reverse_time'], processing_options['time_slices_per_iteration']))

    # holds some acquisition based metadata.
    for time_index, time_range in enumerate(time_ranges):

        raw_data = None

        if query.platform == "LANDSAT_ALL":
            datasets_in = []
            for index in range(len(products)):
                dataset = dc.get_dataset_by_extent(products[index]+query.area_id, product_type=None, platform=platforms[index], time=time_range, longitude=lon_range, latitude=lat_range, measurements=measurements)
                if 'time' in dataset:
                    datasets_in.append(dataset.copy(deep=True))
                dataset = None
            if len(datasets_in) > 0:
                raw_data = xr.concat(datasets_in, 'time')
        else:
            raw_data = dc.get_dataset_by_extent(query.product, product_type=None, platform=query.platform, time=time_range, longitude=lon_range, latitude=lat_range, measurements=measurements)

        # get the actual data and perform analysis.
        if raw_data is None or "cf_mask" not in raw_data:
            continue

        clean_mask = create_cfmask_clean_mask(raw_data.cf_mask)

        wofs_data = processing_options['processing_method'](raw_data, clean_mask=clean_mask, enforce_float64=True)
        water_analysis = perform_timeseries_analysis_iterative(wofs_data, intermediate_product=water_analysis)
        #filter for swir2<1%, where valid range=[0,10000] so 1%=100.* scale of 0.0001
        clean_mask[raw_data.swir2.values > 100] = False
        #manually set all non-water pixels to nodata.
        clean_mask[wofs_data.wofs.values == 0] = False
        tsm_data = tsm(raw_data, clean_mask=clean_mask, no_data=-1)
        tsm_analysis = perform_timeseries_analysis_iterative(tsm_data, intermediate_product=tsm_analysis, no_data=-1)

        # here the clear mask has all the clean pixels for each acquisition.
        # add to the comma seperated list of data.
        for timeslice in range(clean_mask.shape[0]):
            time = raw_data.time.values[timeslice] if type(raw_data.time.values[timeslice]) == datetime.datetime else datetime.datetime.utcfromtimestamp(raw_data.time.values[timeslice].astype(int) * 1e-9)
            clean_pixels = np.sum(clean_mask[timeslice, :, :] == True)
            if time not in acquisition_metadata:
                acquisition_metadata[time] = {}
                acquisition_metadata[time]['clean_pixels'] = 0
            acquisition_metadata[time]['clean_pixels'] += clean_pixels

            # create the files requied for animation..
            # if the dir doesn't exist, create it, then fill with a .png/.tif
            # from the scene data.
            if query.animated_product != "None":
                animated_product = AnimationType.objects.get(
                    type_id=query.animated_product)
                animated_data = tsm_data.isel(time=timeslice).drop(
                    "time") if animated_product.type_id == "scene" else tsm_analysis
                if not os.path.exists(base_temp_path + query.query_id):
                    return None
                if not os.path.exists(base_temp_path + query.query_id + '/' + str(time_num)):
                    os.mkdir(base_temp_path + query.query_id +
                             '/' + str(time_num))
                animated_data.to_netcdf(base_temp_path + query.query_id + '/' + str(
                    time_num) + '/' + str(chunk_num) + str(time_index + timeslice) + ".nc")

    # Save this geographic chunk to disk.
    geo_path = base_temp_path + query.query_id + "/geo_chunk_" + \
        str(time_num) + "_" + str(chunk_num)
    if water_analysis is None or not os.path.exists(base_temp_path + query.query_id):
        return None
    water_analysis.to_netcdf(geo_path + "_water.nc")
    tsm_analysis.to_netcdf(geo_path + "_tsm.nc")
    print("Done with chunk: " + str(time_num) + " " + str(chunk_num))
    return [geo_path + "_water.nc", geo_path + "_tsm.nc", acquisition_metadata]


# Datacube instance to be initialized.
# A seperate DC instance is created for each worker.
dc = None

# Init/shutdown functions for handling dc instances.
# this is done to prevent synchronization/conflicts between workers when
# accessing DC resources.
@worker_process_init.connect
def init_worker(**kwargs):
    print("Creating DC instance for worker.")
    global dc
    dc = DataAccessApi()


@worker_process_shutdown.connect
def shutdown_worker(**kwargs):
    print('Closing DC instance for worker.')
    global dc
    dc = None