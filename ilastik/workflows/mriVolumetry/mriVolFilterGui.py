import os
import itertools
from functools import partial
from collections import OrderedDict, defaultdict

from PyQt4 import uic
from PyQt4.QtCore import Qt, QEvent
from PyQt4.QtGui import QColor, QMessageBox, QListView, QStandardItemModel, \
    QStandardItem, QPixmap, QIcon, QMenu

from ilastik.applets.layerViewer.layerViewerGui import LayerViewerGui
from ilastik.utility.gui import threadRouted
from ilastik.workflows.mriVolumetry.opSmoothing import getMaxSigma

from volumina.api import LazyflowSource, AlphaModulatedLayer, ColortableLayer
# from volumina.colortables import create_default_16bit
from volumina.utility import encode_from_qstring, decode_to_qstring
from volumina.colortables import create_default_16bit

from lazyflow.operators import OpMultiArraySlicer

import numpy as np

import logging
logger = logging.getLogger(__name__)

smoothing_methods_map = ['gaussian', 'guided', 'opengm']


class MriVolFilterGui(LayerViewerGui):

    def stopAndCleanUp(self):
        super(MriVolFilterGui, self).stopAndCleanUp()
        for fn in self.__cleanup_fns:
            fn()

    def __init__(self, *args, **kwargs):
        self.applet = args[0]
        self.__cleanup_fns = []
        self._channelColors = self._createDefault16ColorColorTable()
        self._originalLabels = defaultdict(lambda: None)
        # some markers that keep track of the GUI's state
        self._disable_label_changes = False
        self._ready_for_layers = False
        super(MriVolFilterGui, self).__init__(*args, **kwargs)
        #  use default colors
        # self._channelColors = create_default_16bit()
        # self._channelColors[0] = 0 # make first channel transparent

    def initAppletDrawerUi(self):
        """
        Reimplemented from LayerViewerGui base class.
        """
        # Load the ui file (find it in our own directory)
        localDir = os.path.split(__file__)[0]

        self._drawer = uic.loadUi(localDir+"/filter_drawer.ui")

        op = self.topLevelOperatorView
        # set tabs enabled only for available smoothers
        for i, name in enumerate(smoothing_methods_map):
            if name not in op.methods_available:
                self._drawer.tabWidget.setTabEnabled(i, False)

        # TODO extend the watched widgets list
        self._allWatchedWidgets = [self._drawer.sigmaSpinBox,
                                   self._drawer.thresSpinBox]

        # If the user pressed enter inside a spinbox, auto-click "Apply"
        for widget in self._allWatchedWidgets:
            widget.installEventFilter(self)

        # prepare the label table
        self.model = QStandardItemModel(self._drawer.labelListView)

        # see if we need to update our labels from the operator (i.e.,
        # we are restored from a project file)
        # FIXME find better way of determining this
        deserializing = op.ReassignedObjects.ready()

        if deserializing:
            self._onDeserialization()
        else:
            self._onCreation()

        # connect callbacks last to avoid collision
        self._connectCallbacks()

    def eventFilter(self, watched, event):
        """
        If the user pressed 'enter' within a spinbox,
        auto-click the "apply" button.
        """
        if watched in self._allWatchedWidgets:
            if event.type() == QEvent.KeyPress and\
              (event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return):
                self._drawer.applyButton.click()
                return True
        return False

    # =================================================================
    #                   USER INTERACTION WITH CCs
    # =================================================================

    @staticmethod
    def _getObjectHelper(slot, pos5d):
        slicing = tuple(slice(i, i+1) for i in pos5d)
        arr = slot[slicing].wait()
        return arr.flat[0]

    def _getPossibleLabels(self):
        '''
        returns a OrderdDict containing name and id of labels
        that can be used for assignment
        '''
        labels = OrderedDict()
        labels[0] = 'Background'
        op = self.topLevelOperatorView
        states = op.ActiveChannels.value
        names = list(op.LabelNames.value)
        for i,n in enumerate(names):
            if states[i] > 0:
                labels[i+1] = n
        return labels

    def _getOriginalLabel(self, object_id):
        label = self._originalLabels[object_id]
        return label

    def _assignNewLabel(self, pos5d, object_id, new_label, original_label):
        op = self.topLevelOperatorView
        slicing = tuple(slice(i, i+1) for i in pos5d)
        self._originalLabels[(pos5d[0],object_id)] = original_label
        op.AssignChannelForObject[slicing] = new_label
        op.ReassignedObjects.setValue(dict(self._originalLabels))

    def handleEditorLeftClick(self, position5d, globalWindowCoordinate):
        op = self.topLevelOperatorView

        # get list of possible channel names and labels
        labels = self._getPossibleLabels()

        # get current channel
        current = self._getObjectHelper(op.CachedOutput, position5d)

        t = position5d[0]
        # check if channel was changed
        object_id = self._getObjectHelper(op.ObjectIds, position5d)
        original_label = self._getOriginalLabel((t,object_id))
        if original_label is None:
            original_label = current

        # launch menu
        menu = QMenu(self)
        submenu = QMenu(self)
        submenu.setTitle('Assign membership to')
        for k in labels:
            if k != current and k != original_label: 
                submenu.addAction(labels[k],
                                  partial(self._assignNewLabel, position5d,
                                          object_id, k, original_label))
        menu.addMenu(submenu)

        if current != original_label:
            orig_name = labels[original_label]
            org_member = 'Restore original membership ({})'.format(orig_name)
            menu.addAction(org_member,
                           partial(self._assignNewLabel, position5d,
                                   object_id, original_label, original_label))

        menu.exec_(globalWindowCoordinate)

    def handleEditorRightClick(self, position5d, globalWindowCoordiante):
        pass

    # =================================================================
    #                      LAYER MANIPULATION
    # =================================================================

    def setupLayers(self):
        layers = []
        op = self.topLevelOperatorView

        # don't show output if operator is not configured
        out_ready = self._ready_for_layers

        if op.Output.ready() and out_ready:
            outputLayer = ColortableLayer(LazyflowSource(op.CachedOutput),
                                          colorTable=self._channelColors)
            outputLayer.name = "Output"
            outputLayer.visible = True
            outputLayer.opacity = 0.7
            layers.append(outputLayer)

        if op.ArgmaxOutput.ready() and out_ready:
            outLayer = ColortableLayer(LazyflowSource(op.ArgmaxOutput),
                                       colorTable=self._channelColors)
            outLayer.name = "Argmax"
            outLayer.visible = False
            outLayer.opacity = 1.0
            layers.append(outLayer)

        if op.ObjectIds.ready() and out_ready:
            outLayer = ColortableLayer(LazyflowSource(op.ObjectIds),
                                       colorTable=create_default_16bit())
            outLayer.name = "ObjectIds"
            outLayer.visible = False
            outLayer.opacity = 1.0
            layers.append(outLayer)

        if op.Smoothed.ready() and out_ready:
            numChannels = op.Smoothed.meta.getTaggedShape()['c']
            slicer = OpMultiArraySlicer(
                parent=op.Smoothed.getRealOperator().parent)
            slicer.Input.connect(op.Smoothed)
            slicer.AxisFlag.setValue('c')  # slice along c

            for i in range(numChannels):
                # slicer maps each channel to a subslot of slicer.Slices
                # i.e. slicer.Slices is not really slot, but a list of slots
                channelSrc = LazyflowSource(slicer.Slices[i])
                inputChannelLayer = AlphaModulatedLayer(
                    channelSrc,
                    tintColor=QColor(self._channelColors[i+1]),
                    range=(0.0, 1.0),
                    normalize=(0.0, 1.0))
                inputChannelLayer.opacity = 0.5
                inputChannelLayer.visible = False
                try:
                    labelName = op.LabelNames.value[i]
                except IndexError:
                    labelName = "Unknown class {}".format(i)
                inputChannelLayer.name = decode_to_qstring(labelName)
                inputChannelLayer.setToolTip(decode_to_qstring(
                    "Smoothed predictions for label '{}'".format(labelName)))
                layers.append(inputChannelLayer)

        # raw layer
        if op.RawInput.ready():
            rawLayer = self.createStandardLayerFromSlot(op.RawInput)
            rawLayer.name = "Raw data"
            rawLayer.visible = True
            rawLayer.opacity = 1.0
            layers.append(rawLayer)
        return layers

    def getLayer(self, name):
        """
        find a layer by its name
        """
        try:
            layer = itertools.ifilter(
                lambda l: l.name == name, self.layerstack).next()
        except StopIteration:
            return None
        else:
            return layer

    # =================================================================
    #                    DATA TRANSFER FUNCTIONS
    # =================================================================

    @threadRouted
    def _setLabelList(self, numLabels=0, names=None, active=None):
        op = self.topLevelOperatorView
        if names is not None:
            try:
                n = len(names)
            except TypeError:
                names = [names]
            assert len(names) >= numLabels, "Too few labels provided"
        if active is not None:
            try:
                n = len(active)
            except TypeError:
                active = [active]
            assert len(active) >= numLabels, "Too few booleans provided"

        # trash current model
        self.model.clear()

        # fill model with entries
        for i in range(numLabels):
            item = QStandardItem()
            if names is not None:
                item_name = names[i]
            else:
                item_name = 'Prediction {}'.format(i+1)
            item.setText(decode_to_qstring(item_name))
            item.setCheckable(True)

            if active is not None:
                item.setCheckState(2 if active[i] else 0)
            else:
                # check all classes by default
                item.setCheckState(2)

            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(self._channelColors[i+1]))
            item.setIcon(QIcon(pixmap))

            # don't allow label name changes if upstream present
            item.setEditable(op.LabelNames.partner is None)

            self.model.appendRow(item)

        self._drawer.labelListView.setModel(self.model)

    def _setLabelNamesToOp(self):
        op = self.topLevelOperatorView

        # check if we need to do stuff
        if op.LabelNames.partner is not None:
            return

        new_names = [encode_from_qstring(self.model.item(i).text())
                     for i in range(self.model.rowCount())]
        new_names = np.array(new_names, dtype=np.object)

        # update the layers
        # the layers are not updated by setting op.LabelNames,
        # probably because no output is set dirty, which is correct
        # BUT we do not want to recreate the layers anyway, just rename
        # them
        if op.LabelNames.ready():
            old_names = op.LabelNames.value
            for old, new in zip(map(decode_to_qstring, old_names),
                                map(decode_to_qstring, new_names)):
                layer = self.getLayer(old)
                if layer is not None:
                    layer.name = new
        op.LabelNames.setValue(new_names)

    def _setActiveChannelsToOp(self):
        op = self.topLevelOperatorView
        new_states = [self.model.item(i).checkState()
                      for i in range(self.model.rowCount())]
        new_states = np.array(new_states, dtype=int)
        op.ActiveChannels.setValue(new_states)

    def _setParamsToOp(self):
        op = self.topLevelOperatorView

        tab_index = self._drawer.tabWidget.currentIndex()
        conf = self._getTabConfig()

        op.Method.setValue(smoothing_methods_map[tab_index])
        op.Configuration.setValue(conf)

        thres = self._drawer.thresSpinBox.value()
        op.Threshold.setValue(thres)

        op.ReassignedObjects.setValue(dict(self._originalLabels))

    @threadRouted
    def _getLabelNamesFromOp(self):
        op = self.topLevelOperatorView
        # do we have label names? if not, then create empty list
        if not op.LabelNames.ready():
            self._setLabelList()
            return
        names = op.LabelNames.value

        # do we have a change in size? If yes, then replace label list
        oldNumLabels = self.model.rowCount()
        numLabels = len(names)
        if oldNumLabels != numLabels:
            self._setLabelList(numLabels=numLabels, names=names)
            # we need to reset the active channels, too
            self._setActiveChannelsToOp()
            return

        # we can assume that only a label name changed
        # update the channel list
        for i in range(len(names)):
            self.model.item(i).setText(decode_to_qstring(names[i]))

    @threadRouted
    def _getActiveChannelsFromOp(self):
        op = self.topLevelOperatorView
        # update the channel list
        states = op.ActiveChannels.value
        for i in range(min(self.model.rowCount(), len(states))):
            self.model.item(i).setCheckState(int(states[i]))

    @threadRouted
    def _getParamsFromOp(self):
        # Set Maximum Value of Sigma
        # FIXME does not support 2d with explicit z, for example
        op = self.topLevelOperatorView
        ts = op.Input.meta.getTaggedShape()
        shape = [ts[k] for k in ts if k in 'xyz']
        max_sigma = getMaxSigma(shape)
        self._drawer.sigmaSpinBox.setMaximum(max_sigma)
        self._drawer.sigmaGuidedSpinBox.setMaximum(max_sigma)
        self._drawer.sigmaGMSpinBox.setMaximum(max_sigma)

        thres = self._drawer.thresSpinBox.value()
        thres = op.Threshold.value
        self._drawer.thresSpinBox.setValue(thres)
        self._spinbox_value_changed(thres)

        method = op.Method.value
        try:
            i = smoothing_methods_map.index(method)
        except ValueError:
            logger.warn("Smoothing method '{}' unknown to GUI, "
                        "using default...".format(method))
            i = 0
        self._drawer.tabWidget.setCurrentIndex(i)
        self._setTabConfig(op.Configuration.value)

        if op.ReassignedObjects.ready():
            self._originalLabels = defaultdict(
                lambda: None, op.ReassignedObjects.value)

    # =================================================================
    #                       HELPER FUNCTIONS
    # =================================================================

    def _getTabConfig(self):
        tab_index = self._drawer.tabWidget.currentIndex()

        if tab_index == 0:
            sigma = self._drawer.sigmaSpinBox.value()
            conf = {'sigma': sigma}
        elif tab_index == 1:
            eps = self._drawer.epsGuidedSpinBox.value()
            sigma = self._drawer.sigmaGuidedSpinBox.value()
            guided = self._drawer.guidedCheckBox.isChecked()
            conf = { 'sigma': sigma,
                     'eps': eps ,
                     'guided' : guided}
        elif tab_index == 2:
            sigma = self._drawer.sigmaGMSpinBox.value()
            unaries = self._drawer.unariesGMSpinBox.value()
            conf = { 'sigma': sigma,
                     'unaries': unaries }
        else:
            raise ValueError('Unknown tab {} selected'.format(tab_index))
        return conf

    def _setTabConfig(self, conf):
        try:
            tab_index = self._drawer.tabWidget.currentIndex()
            ## all methods contain sigma, the same max value applies
            sigma = conf['sigma']
            sigma = min(sigma, self._drawer.sigmaSpinBox.maximum())
            sigma = max(sigma, self._drawer.sigmaSpinBox.minimum())
            if sigma != conf['sigma']:
                logger.warn("Could not apply sigma {} because of "
                            "operator restrictions".format(conf['sigma']))
                self._params_valid = False
            if tab_index == 0:
                self._drawer.sigmaSpinBox.setValue(sigma)
            elif tab_index == 1:
                # TODO check range of allowed values
                self._drawer.epsGuidedSpinBox.setValue(conf['eps'])
                self._drawer.sigmaGuidedSpinBox.setValue(conf['sigma'])
                if conf['guide']:
                    self._drawer.guidedCheckBox.setChecked(2)
                else:
                    self._drawer.guidedCheckBox.setChecked(0)
            elif tab_index == 2:
                self._drawer.sigmaGMSpinBox.setValue(conf['sigma'])
                self._drawer.unariesGMSpinBox.setValue(conf['unaries'])
            else:
                raise ValueError(
                    'Unknown tab {} selected'.format(tab_index))
        except KeyError:
            logger.warn("Bad smoothing configuration encountered")
            params_valid = False

    def _getLabelDetails(self):
        op = self.topLevelOperatorView

        if op.Input.ready():
            n = op.Input.meta.getTaggedShape()['c']
        else:
            n = 0
        if op.LabelNames.ready():
            names = op.LabelNames.value
        else:
            names = None
        if op.ActiveChannels.ready():
            active = op.ActiveChannels.value
        else:
            active = None
        return n, names, active

    # =================================================================
    #                          CALLBACKS
    # =================================================================

    def _connectCallbacks(self):
        op = self.topLevelOperatorView

        # GUI is recreated every time the input changes, so we don't need to
        # trap that case
        # op.Input.notifyMetaChanged(self._onInputChanged)
        # self.__cleanup_fns.append(
        #     lambda: op.Input.unregisterMetaChanged(self._onInputChanged))

        self._drawer.applyButton.clicked.connect(self._onApplyButtonClicked)

        # syncronize slider and spinbox
        self._drawer.slider.valueChanged.connect(self._slider_value_changed)
        self._drawer.thresSpinBox.valueChanged.connect(
            self._spinbox_value_changed)

        # HACK we are connecting to sig_meta_changed because
        # sig_value_changed is never called, see issue:
        #   github.com/ilastik/lazyfow/issues/155
        op.LabelNames.notifyMetaChanged(self._onLabelsChanged)
        self.__cleanup_fns.append(
            lambda: op.Input.unregisterMetaChanged(self._onLabelsChanged))

        self.model.itemChanged.connect(self._onGuiLabelsChanged)

    def _onInputChanged(self, *args, **kwargs):
        '''
        call this method whenever the top level operators input changes
        '''
        class UnexpectedStuffHappening(Exception):
            pass
        raise UnexpectedStuffHappening("input changed mysteriously")

    def _onGuiLabelsChanged(self, *args, **kwargs):
        # apply new labels to GUI, ignore labels_changed callback until
        # we are done
        # FIXME race condition?
        self._disable_label_changes = True
        self._setLabelNamesToOp()
        self._disable_label_changes = False

    def _onLabelsChanged(self, *args, **kwargs):
        # apply new labels to GUI
        if self._disable_label_changes:
            return
        self._getLabelNamesFromOp()
        self.updateAllLayers()

    def _onApplyButtonClicked(self, *args, **kwargs):
        '''
        updates the top level operator with GUI provided values
        '''
        self._setActiveChannelsToOp()
        self._setParamsToOp()
        self._ready_for_layers = True
        self.updateAllLayers()
        self.applet.appletStateUpdateRequested.emit()

    def _onCreation(self):
        logger.debug("Creating MriVolFilterGui")
        op = self.topLevelOperatorView

        # get default parameters from op
        self._getParamsFromOp()
        # set the parameters so that invalid parameters get overwritten
        # this also sets the ReassignedObjects slot
        self._setParamsToOp()
        n, names, active = self._getLabelDetails()

        # fill the label list -> creates label names if unavailable
        self._setLabelList(numLabels=n, names=names, active=active)
        self._setLabelNamesToOp()
        self._setActiveChannelsToOp()

    def _onDeserialization(self):
        logger.debug("Deserializing MriVolFilterGui")
        op = self.topLevelOperatorView
        n, names, active = self._getLabelDetails()
        self._setLabelList(numLabels=n, names=names, active=active)
        self._getParamsFromOp()
        self._ready_for_layers = op.CachedOutput.ready()
        self.updateAllLayers()

    def _slider_value_changed(self, value):
        self._drawer.thresSpinBox.setValue(value)

    def _spinbox_value_changed(self, value):
        self._drawer.slider.setValue(value)
    

    # =================================================================
    #                         STATIC METHODS
    # =================================================================


    @staticmethod
    def _createDefault16ColorColorTable():
        colors = []

        # SKIP: Transparent for the zero label
        colors.append(QColor(0,0,0,0))

        # ilastik v0.5 colors
        colors.append( QColor( Qt.red ) )
        colors.append( QColor( Qt.green ) )
        colors.append( QColor( Qt.yellow ) )
        colors.append( QColor( Qt.blue ) )
        colors.append( QColor( Qt.magenta ) )
        colors.append( QColor( Qt.darkYellow ) )
        colors.append( QColor( Qt.lightGray ) )

        # Additional colors
        colors.append( QColor(255, 105, 180) ) #hot pink
        colors.append( QColor(102, 205, 170) ) #dark aquamarine
        colors.append( QColor(165,  42,  42) ) #brown
        colors.append( QColor(0, 0, 128) )     #navy
        colors.append( QColor(255, 165, 0) )   #orange
        colors.append( QColor(173, 255,  47) ) #green-yellow
        colors.append( QColor(128,0, 128) )    #purple
        colors.append( QColor(240, 230, 140) ) #khaki

        colors.append( QColor(192, 192, 192) ) #silver

#        colors.append( QColor(69, 69, 69) )    # dark grey
#        colors.append( QColor( Qt.cyan ) )

        assert len(colors) == 17
        return [c.rgba() for c in colors]