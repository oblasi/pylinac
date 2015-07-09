# -*- coding: utf-8 -*-
"""
The Starshot module analyses a starshot film or multiple superimposed EPID images that measures the wobble of the
radiation spokes, whether gantry, collimator, or couch. It is based on ideas from
`Depuydt et al <http://iopscience.iop.org/0031-9155/57/10/2997>`_
and `Gonzalez et al <http://dx.doi.org/10.1118/1.1755491>`_ and evolutionary optimization.
"""

import os.path as osp
import copy
from io import BytesIO

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import differential_evolution

from pylinac.core.decorators import value_accept
from pylinac.core.geometry import Point, Line, Circle
from pylinac.core.image import Image
from pylinac.core.io import get_filepath_UI, get_filenames_UI
from pylinac.core.profile import SingleProfile, CollapsedCircleProfile


class Starshot:
    """Class that can determine the wobble in a "starshot" image, be it gantry, collimator,
        couch or MLC. The image can be DICOM or a scanned film (TIF, JPG, etc).

    Attributes
    ----------
    image : :class:`~pylinac.core.image.Image`
    circle_profile : :class:`~pylinac.starshot.StarProfile`
    lines : :class:`~pylinac.starshot.LineManager`
    wobble : :class:`~pylinac.starshot.Wobble`

    Examples
    --------
    Run the demo:
        >>> Starshot().run_demo()

    Typical session:
        >>> img_path = r"C:/QA/Starshots/Coll.jpeg"
        >>> mystar = Starshot(img_path)
        >>> mystar.analyze()
        >>> print(mystar.return_results())
        >>> mystar.plot_analyzed_image()
    """
    def __init__(self, filepath=None):
        """
        Parameters
        ----------
        filepath : str, optional
            The path to the image file. If None, the image must be loaded later.
        """
        self.wobble = Wobble()
        self.tolerance = Tolerance(1, 'pixels')
        if filepath is not None:
            self.load_image(filepath)

    @classmethod
    def from_url(cls, url):
        """Instantiate from a URL.

        .. versionadded:: 0.7.1
        """
        obj = cls()
        obj.load_url(url)
        return obj

    def load_url(self, url):
        """Load from a URL.

        .. versionadded:: 0.7.1
        """
        try:
            import requests
        except ImportError:
            raise ImportError("Requests is not installed; cannot get the log from a URL")
        response = requests.get(url)
        if response.status_code != 200:
            raise ConnectionError("Could not connect to the URL")
        stream = BytesIO(response.content)
        self.load_image(stream)

    @classmethod
    def from_demo_image(cls):
        """Construct a Starshot instance and load the demo image.

        .. versionadded:: 0.6
        """
        obj = cls()
        obj.load_demo_image()
        return obj

    def load_demo_image(self):
        """Load the starshot demo image."""
        demo_file = osp.join(osp.dirname(__file__), 'demo_files', 'starshot', '10X_collimator.tif')
        self.load_image(demo_file)

    def load_image(self, filepath):
        """Load the image via the file path.

        Parameters
        ----------
        filepath : str
            Path to the file to be loaded.
        """
        self.image = Image(filepath)

    @classmethod
    def from_multiple_images(cls, filepath_list):
        """Construct a Starshot instance and load in and combine multiple images.

        .. versionadded:: 0.6

        Parameters
        ----------
        filepath_list : iterable
            An iterable of file paths to starshot images that are to be superimposed.
        """
        obj = cls()
        obj.load_multiple_images(filepath_list)
        return obj

    def load_multiple_images(self, filepath_list):
        """Load multiple images via the file path.

        .. versionadded:: 0.5.1

        Parameters
        ----------
        filepath_list : sequence
            An iterable sequence of filepath locations.
        """
        self.image = Image.from_multiples(filepath_list)

    @classmethod
    def from_multiple_images_UI(cls):
        """Construct a Starshot instance and load in and combine multiple images via a UI dialog box.

        .. versionadded:: 0.6
        """
        obj = cls()
        obj.load_multiple_images_UI()
        return obj

    def load_multiple_images_UI(self):
        """Load multiple images via a dialog box.

        .. versionadded:: 0.5.1
        """
        path_list = get_filenames_UI()
        if path_list:
            self.load_multiple_images(path_list)

    @classmethod
    def from_image_UI(cls):
        """Construct a Starshot instance and get the image via a UI dialog box.

        .. versionadded:: 0.6
        """
        obj = cls()
        obj.load_image_UI()
        return obj

    def load_image_UI(self):
        """Load the image by using a UI dialog box."""
        path = get_filepath_UI()
        if path:
            self.load_image(path)

    def _check_image_inversion(self):
        """Check the image for proper inversion, i.e. that pixel value increases with dose."""
        # sum the image along each axis
        x_sum = np.sum(self.image.array, 0)
        y_sum = np.sum(self.image.array, 1)

        # determine the point of max value for each sum profile
        xmaxind = np.argmax(x_sum)
        ymaxind = np.argmax(y_sum)

        # If that maximum point isn't near the center (central 1/3), invert image.
        center_in_central_third = ((xmaxind > len(x_sum) / 3 and xmaxind < len(x_sum) * 2 / 3) and
                               (ymaxind > len(y_sum) / 3 and ymaxind < len(y_sum) * 2 / 3))
        if not center_in_central_third:
            self.image.invert()

    def _get_reasonable_start_point(self):
        """Set the algorithm starting point automatically.

        Notes
        -----
        The determination of an automatic start point is accomplished by finding the Full-Width-80%-Max.
        Finding the maximum pixel does not consistently work, esp. in the presence of a pin prick. The
        FW80M is a more consistent metric for finding a good start point.
        """
        # sum the image along each axis within the central 1/3 (avoids outlier influence from say, gantry shots)
        top_third = int(self.image.array.shape[0]/3)
        bottom_third = int(top_third * 2)
        left_third = int(self.image.array.shape[1]/3)
        right_third = int(left_third * 2)
        central_array = self.image.array[top_third:bottom_third, left_third:right_third]

        x_sum = np.sum(central_array, 0)
        y_sum = np.sum(central_array, 1)

        # Calculate Full-Width, 80% Maximum
        fwxm_x_point = SingleProfile(x_sum).get_FWXM_center(80) + left_third
        fwxm_y_point = SingleProfile(y_sum).get_FWXM_center(80) + top_third

        # find maximum points
        x_max = np.unravel_index(np.argmax(central_array), central_array.shape)[1] + left_third
        y_max = np.unravel_index(np.argmax(central_array), central_array.shape)[0] + top_third

        # which one is closer to the center
        fwxm_dist = Point(fwxm_x_point, fwxm_y_point).dist_to(self.image.center)
        max_dist = Point(x_max, y_max).dist_to(self.image.center)

        if fwxm_dist < max_dist:
            center_point = Point(fwxm_x_point, fwxm_y_point)
        else:
            center_point = Point(x_max, y_max)

        return center_point

    @value_accept(radius=(0.2, 0.95), min_peak_height=(0.05, 0.95), SID=(40, 400))
    def analyze(self, radius=0.85, min_peak_height=0.25, tolerance=1.0, SID=100, start_point=None, fwhm=True, recursive=True):
        """Analyze the starshot image.

        Analyze finds the minimum radius and center of a circle that touches all the lines
        (i.e. the wobble circle diameter and wobble center).

        Parameters
        ----------
        radius : float, optional
            Distance in % between starting point and closest image edge; used to build the circular profile which finds
            the radiation lines. Must be between 0.05 and 0.95.
        min_peak_height : float, optional
            The percentage minimum height a peak must be to be considered a valid peak. A lower value catches
            radiation peaks that vary in magnitude (e.g. different MU delivered or gantry shot), but could also pick up noise.
            If necessary, lower value for gantry shots and increase for noisy images.
        tolerance : int, float, optional
            The tolerance to test against for a pass/fail result. If the image has a pixel/mm conversion factor, the tolerance is in mm.
            If the image has not conversion factor, the tolerance is in pixels.
        SID : int, float, optional
            The source-to-image distance in cm. If a value != 100 is passed in, results will be scaled to 100cm. E.g. a wobble of
            3.0 pixels at an SID of 150cm will calculate to 2.0 pixels [3 / (150/100)].

            .. note::
                For EPID images (e.g. superimposed collimator shots), the SID is in the DICOM file and this
                value will always be used if it can be found, otherwise the passed value will be used.
        start_point : 2-element iterable, optional
            A point where the algorithm should use for determining the circle profile.
            If None (default), will search for a reasonable maximum point nearest the center of the image.
        fwhm : bool
            If True (default), the center of the FWHM of the spokes will be determined.
            If False, the peak value location is used as the spoke center.

            .. note:: In practice, this ends up being a very small difference. Set to false if peak locations are offset or unexpected.
        recursive : bool
            If True (default), will recursively search for a "reasonable" wobble, meaning the wobble radius is
            <3mm. If the wobble found was unreasonable,
            the minimum peak height is iteratively adjusted from low to high at the passed radius.
            If for all peak heights at that point the wobble is still unreasonable, the
            radius is then iterated over from most distant inward.
            If False, will simply return the first determined value or raise error if a reasonable wobble could not be determined.

            .. warning:: It is strongly recommended to leave this setting at True, unless you have a strong reason.

        Raises
        ------
        AttributeError
            If an image has not yet been loaded.
        RuntimeError
            If a reasonable wobble value was not found.
        """
        if not self.image_is_loaded:
            raise AttributeError("Starshot image not yet loaded")

        self.tolerance.value = tolerance
        self._check_image_inversion()

        if start_point is None:
            start_point = self._get_reasonable_start_point()

        self._get_reasonable_wobble(start_point, SID, fwhm, min_peak_height, radius, recursive)

    def _get_reasonable_wobble(self, start_point, SID, fwhm, min_peak_height, radius, recursive):
        """Determine a wobble that is "reasonable". If recursive is false, the first iteration will be passed,
        otherwise the parameters will be tweaked to search for a reasonable wobble."""
        wobble_unreasonable = True
        focus_point = copy.copy(start_point)
        peak_gen = get_peak_height()
        radius_gen = get_radius()
        while wobble_unreasonable:
            try:
                self.circle_profile = StarProfile(self.image, focus_point, radius, min_peak_height, fwhm)
                if len(self.circle_profile.peaks) < 6:
                    raise ValueError
                self.lines = LineManager(self.circle_profile.peaks)
                self._find_wobble_minimize(SID)
            except ValueError:
                if not recursive:
                    raise RuntimeError("The algorithm was unable to properly detect the radiation lines. Try setting "
                                       "recursive to True or lower the minimum peak height")
            finally:
                # set the focus point to the wobble minimum
                focus_point = self.wobble.center
                # stop after first iteration if not recursive
                if not recursive:
                    wobble_unreasonable = False
                # otherwise, check if the wobble is reasonable
                else:
                    # if so, stop
                    if self.wobble.radius_mm < 3:
                        wobble_unreasonable = False
                    # otherwise, iterate through peak height
                    else:
                        try:
                            min_peak_height = next(peak_gen)
                        except StopIteration:
                            # if no height setting works, change the radius and reset the height
                            try:
                                radius = next(radius_gen)
                                peak_gen = get_peak_height()
                            except StopIteration:
                                raise RuntimeError("The algorithm was unable to determine a reasonable wobble. Try setting "
                                                   "recursive to False and manually adjusting algorithm parameters")

    @property
    def image_is_loaded(self):
        """Boolean property specifying if an image has been loaded."""
        return hasattr(self.image, 'size')

    def _scale_wobble(self, SID):
        """Scale the determined wobble by the SID.

        Parameters
        ----------
        SID : int, float
            Source to image distance in cm.
        """
        # convert wobble to mm if possible
        if self.image.dpmm is not None:
            self.tolerance.unit = 'mm'
            self.wobble.radius_mm = self.wobble.radius / self.image.dpmm
        else:
            self.tolerance.unit = 'pixels'
            self.wobble.radius_mm = self.wobble.radius

        if self.image.SID is not None:
            self.wobble.radius /= self.image.SID / 100
            self.wobble.radius_mm /= self.image.SID / 100
        else:
            self.wobble.radius /= SID / 100
            self.wobble.radius_mm /= SID / 100

    def _find_wobble_minimize(self, SID):
        """Find the minimum distance wobble location and radius to all radiation lines."""
        sp = copy.copy(self.circle_profile.center)

        def distance(p, lines):
            """Calculate the maximum distance to any line from the given point."""
            return max(line.distance_to(Point(p[0], p[1])) for line in lines)

        res = differential_evolution(distance, bounds=[(sp.x*0.95, sp.x*1.05), (sp.y*0.95, sp.y*1.05)], args=(self.lines,))

        self.wobble.radius = res.fun
        self.wobble.center = Point(res.x[0], res.x[1])

        self._scale_wobble(SID)

    @property
    def passed(self):
        """Boolean specifying whether the determined wobble was within tolerance."""
        return self.wobble.radius_mm * 2 < self.tolerance.value

    @property
    def _passfail_str(self):
        """Return a pass/fail string."""
        return 'PASS' if self.passed else 'FAIL'

    def return_results(self):
        """Return the results of the analysis.

        Returns
        -------
        string
            A string with a statement of the minimum circle.
        """
        string = ('\nResult: %s \n\n'
                  'The minimum circle that touches all the star lines has a diameter of %4.3g %s. \n\n'
                  'The center of the minimum circle is at %4.1f, %4.1f') % (self._passfail_str, self.wobble.radius_mm*2, self.tolerance.unit,
                                                                            self.wobble.center.x, self.wobble.center.y)
        return string

    def plot_analyzed_image(self, show=True):
        """Draw the star lines, profile circle, and wobble circle on a matplotlib figure.

        Parameters
        ----------
        show : bool
            Whether to actually show the image.
        """
        plt.clf()
        imgplot = plt.imshow(self.image.array, cmap=plt.cm.Greys)

        self.lines.plot(imgplot.axes)
        self.wobble.add_to_axes(imgplot.axes, edgecolor='green')
        self.circle_profile.add_to_axes(imgplot.axes, edgecolor='green')

        imgplot.axes.autoscale(tight=True)
        imgplot.axes.axis('off')

        if show:
            plt.show()

    def save_analyzed_image(self, filename, **kwargs):
        """Save the analyzed image plot to a file.

        Parameters
        ----------
        filename : str, IO stream
            The filename to save as. Format is deduced from string extention, if there is one. E.g. 'mystar.png' will
            produce a PNG image.

        kwargs
            All other kwargs are passed to plt.savefig().
        """
        self.plot_analyzed_image(show=False)
        plt.savefig(filename, **kwargs)

    def run_demo(self):
        """Demonstrate the Starshot module using the demo image."""
        self.load_demo_image()
        self.analyze()
        print(self.return_results())
        self.plot_analyzed_image()


class Wobble(Circle):
    """A class that holds the wobble information of the Starshot analysis.

    Attributes
    ----------
    radius_mm : The radius of the Circle in **mm**.
    """
    def __init__(self, center_point=None, radius=None):
        super().__init__(center_point=center_point, radius=radius)
        self.radius_mm = 0  # The radius of the wobble in mm; as opposed to pixels.

    @property
    def diameter_mm(self):
        return self.radius_mm*2


class LineManager:
    """Manages the radiation lines found."""
    def __init__(self, points):
        """
        Parameters
        ----------
        points :
            The peak points found by the StarProfile
        """
        self.lines = []
        self.construct_rad_lines(points)

    def __getitem__(self, item):
        return self.lines[item]

    def __len__(self):
        return len(self.lines)

    def construct_rad_lines(self, points):
        """Find and match the positions of peaks in the circle profile (radiation lines)
            and map their positions to the starshot image.

        Radiation lines are found by finding the FWHM of the radiation spokes, then matching them
        to form lines.

        Returns
        -------
        lines : list
            A list of Lines (radiation lines) found.

        See Also
        --------
        Starshot.analyze() : min_peak_height parameter info
        core.profile.CircleProfile.find_FWXM_peaks : min_peak_distance parameter info.
        geometry.Line : returning object
        """
        self.match_points(points)

    def match_points(self, points):
        """Match the peaks found to the same radiation lines.

        Peaks are matched by connecting the existing peaks based on an offset of peaks. E.g. if there are
        12 peaks, there must be 6 radiation lines. Furthermore, assuming star lines go all the way across the CAX,
        the 7th peak will be the opposite peak of the 1st peak, forming a line. This method is robust to
        starting points far away from the real center.
        """
        num_rad_lines = int(len(points) / 2)
        offset = num_rad_lines
        self.lines = [Line(points[line], points[line + offset]) for line in range(num_rad_lines)]

    def plot(self, axis):
        """Plot the lines to the axis."""
        for line in self.lines:
            line.add_to_axes(axis, color='blue')


class StarProfile(CollapsedCircleProfile):
    """Class that holds and analyzes the circular profile which finds the radiation lines."""
    def __init__(self, image, start_point, radius, min_peak_height, fwhm):
        super().__init__(center=start_point, radius=radius)
        self.radius = self._convert_radius_perc2pix(image, start_point, radius)
        self.get_median_profile(image.array)
        # self.filter()
        self.get_peaks(min_peak_height, fwhm=fwhm)

    @staticmethod
    def _convert_radius_perc2pix(image, start_point, radius):
        """Convert a percent radius to distance in pixels, based on the distance from center point to image
            edge.

        Parameters
        ----------
        radius : float
            The radius ratio (e.g. 0.5).
        """
        return image.dist2edge_min(start_point) * radius

    def get_median_profile(self, image_array):
        """Take the profile over the image array. Overloads to also correct for profile positioning.

        See Also
        --------
        :meth:`~pylinac.core.profile.CircleProfile.get_profile` : Further parameter info
        """
        prof_size = 4*self.radius*np.pi
        super().get_profile(image_array, size=prof_size)
        self._roll_prof_to_midvalley()
        self.ground()

    def _roll_prof_to_midvalley(self):
        """Roll the circle profile so that its edges are not near a radiation line.
            This is a prerequisite for properly finding star lines.
        """
        roll_amount = np.where(self.y_values == self.y_values.min())[0][0]
        # Roll the profile and x and y coordinates
        self.y_values = np.roll(self.y_values, -roll_amount)
        self.x_locs = np.roll(self.x_locs, -roll_amount)
        self.y_locs = np.roll(self.y_locs, -roll_amount)
        return roll_amount

    def get_peaks(self, min_peak_height, min_peak_distance=0.02, fwhm=True):
        """Determine the peaks of the profile."""
        if fwhm:
            self.find_FWXM_peaks(fwxm=70, min_peak_height=min_peak_height, min_peak_distance=min_peak_distance)
        else:
            self.find_peaks(min_peak_height, min_peak_distance)


class Tolerance:
    """A class for holding tolerance information."""
    def __init__(self, value=None, unit=None):
        self.value = value
        self.unit = unit


def get_peak_height():
    for height in np.linspace(0.05, 0.95, 10):
        yield height


def get_radius():
    for radius in np.linspace(0.95, 0.1, 10):
        yield radius

# ----------------------------
# Starshot demo
# ----------------------------
if __name__ == '__main__':
    pass
#     import os
#     url = 'https://s3.amazonaws.com/assuranceqa-staging/uploads/imgs/10X_collimator_dvTK5Jc.jpg'
#     star = Starshot.from_url(url)
#     ttt = 1
    # Starshot().run_demo()
